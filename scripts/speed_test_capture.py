#!/usr/bin/env python
"""Prueba de velocidad: captura simultánea de las 4 cámaras del hub USB a su
frame rate máximo en BayerRG8 durante N segundos, y vuelca cada cámara a un
.mp4 en disco.

Objetivo: estresar la placa PCIe + hub USB con el mayor caudal de datos que
las cámaras puedan generar a la vez, y medir el throughput real alcanzado.

Uso:
    python speed_test_capture.py [-d SEGUNDOS] [-o DIR] [-e MICROSEG]
"""
import argparse
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from vmbpy import Camera, Frame, FrameStatus, PixelFormat, Stream, VmbSystem


def available_ram_bytes() -> int:
    """Lee MemAvailable de /proc/meminfo (memoria realmente asignable sin
    empezar a hacer swap, incluye caché reclamable)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except Exception:
        pass
    return 2 * 1024 ** 3  # fallback conservador: asume solo 2GB libres

PIXEL_FORMAT = PixelFormat.BayerRG8
BAYER_TO_BGR = cv2.COLOR_BayerBG2BGR
# Nota: aunque la cámara reporta PixelFormat "BayerRG8", el patrón real
# visto por OpenCV corresponde a su constante "BG" (verificado comparando
# contra la conversión BGR8 nativa del propio SDK) — con "RG2BGR" los
# canales rojo y azul salían intercambiados.
DEFAULT_EXPOSURE_US = 4000.0  # exposición corta y fija para no limitar el fps
DEFAULT_STAGGER_S = 0.3  # separación entre start_streaming() de cada cámara.
# Arrancar los 4 streams USB3 Vision en el mismo instante hace que el hub /
# controlador se niegue a abrir alguno de los canales ("could not be found"),
# aunque el caudal de datos agregado esté muy por debajo de su capacidad
# (confirmado con pruebas: falla igual a 1/4 de resolución). Con ~300ms de
# margen entre cada apertura de canal, las 4 cámaras arrancan sin caídas,
# incluso a resolución completa. Ver scripts/probe_staggered_start.py.


@dataclass
class CamResult:
    cam_id: str
    cam_name: str
    width: int = 0
    height: int = 0
    configured_fps: float = 0.0
    frames: List[np.ndarray] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    incomplete: int = 0
    error: Optional[str] = None


class RawCollector:
    """Handler de streaming: copia cada frame recibido (crudo, sin convertir)
    y re-encola el buffer original inmediatamente para no frenar la cámara.
    El fps real se mide a partir de la marca de tiempo de llegada de cada
    frame, no de temporizadores externos al hilo (más robusto frente a
    jitter de arranque/parada entre las 4 cámaras)."""

    def __init__(self, result: CamResult):
        self.result = result

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            self.result.frames.append(frame.as_numpy_ndarray().copy())
            self.result.timestamps.append(time.perf_counter())
        else:
            self.result.incomplete += 1
        cam.queue_frame(frame)


def configure_camera(cam: Camera, exposure_us: Optional[float], binning: int = 1,
                      throughput_bps: Optional[float] = None,
                      target_fps: Optional[float] = None,
                      guarantee_fps: bool = False,
                      resolution: Optional["tuple[int, int]"] = None) -> float:
    """Configura binning, resolución, pixel format BayerRG8 y frame rate al
    máximo permitido. Devuelve el frame rate configurado.

    `resolution=None` (por defecto) usa la resolución máxima del sensor
    (tras el binning aplicado). Un `(ancho, alto)` concreto recorta el
    sensor a ese tamaño, centrado — útil p.ej. para el recorte a 1936x862
    con el que se alcanzan los 176fps reales de esta cámara (ver README).

    `exposure_us=None` deja la auto-exposición activa (cada cámara ajusta su
    propio brillo según su iluminación real — necesario cuando las 4 cámaras
    no reciben la misma luz, si no algunas salen negras). Un valor numérico
    fija la exposición manualmente (necesario para ir a fps máximo, a costa
    de que la imagen pueda salir sub/sobre-expuesta según la escena)."""
    # Si una sesión anterior dejó la cámara en modo trigger externo
    # (TriggerMode=On, usado por los scripts hw_trigger_*), AcquisitionFrameRate
    # pasa a ser de solo lectura y el resto de esta función falla. Se fuerza
    # siempre a modo libre (software) antes de tocar nada más.
    try:
        cam.TriggerMode.set("Off")
    except Exception:
        pass

    # Se fija siempre explícitamente (incluso a 1): la cámara conserva el
    # binning de la sesión anterior si no se sobreescribe.
    try:
        cam.BinningHorizontal.set(binning)
        cam.BinningVertical.set(binning)
    except Exception:
        pass

    # Al cambiar el binning, WidthMax/HeightMax cambian, pero Width/Height no
    # se reajustan solos: si venían recortados de una sesión anterior se
    # quedan así. Se fuerzan siempre, a la resolución máxima disponible o a
    # la pedida por el usuario.
    try:
        # Los offsets se resetean primero: si no, limitan el rango máximo
        # que luego se le puede pedir a Width/Height.
        cam.OffsetX.set(0)
        cam.OffsetY.set(0)

        if resolution is None:
            cam.Width.set(cam.WidthMax.get())
            cam.Height.set(cam.HeightMax.get())
        else:
            req_w, req_h = resolution
            wlo, whi = cam.Width.get_range()
            hlo, hhi = cam.Height.get_range()
            w_inc = cam.Width.get_increment()
            h_inc = cam.Height.get_increment()
            w = min(max(req_w, wlo), whi)
            w -= (w - wlo) % w_inc
            h = min(max(req_h, hlo), hhi)
            h -= (h - hlo) % h_inc
            cam.Width.set(w)
            cam.Height.set(h)

            # Centra el recorte en el sensor en vez de dejarlo anclado
            # arriba a la izquierda.
            ox_lo, ox_hi = cam.OffsetX.get_range()
            oy_lo, oy_hi = cam.OffsetY.get_range()
            ox_inc = cam.OffsetX.get_increment()
            oy_inc = cam.OffsetY.get_increment()
            ox = ox_lo + ((ox_hi - ox_lo) // 2 // ox_inc) * ox_inc
            oy = oy_lo + ((oy_hi - oy_lo) // 2 // oy_inc) * oy_inc
            cam.OffsetX.set(ox)
            cam.OffsetY.set(oy)
    except Exception:
        pass

    if throughput_bps is not None:
        try:
            tlo, thi = cam.DeviceLinkThroughputLimit.get_range()
            cam.DeviceLinkThroughputLimit.set(min(max(throughput_bps, tlo), thi))
        except Exception:
            pass

    cam.set_pixel_format(PIXEL_FORMAT)

    if exposure_us is None and guarantee_fps and target_fps:
        # AcquisitionFrameRate es un TECHO, no un valor garantizado: si la
        # auto-exposición decide que necesita más tiempo del que el fps
        # objetivo permite, el fps real cae por debajo (lo que se veía
        # antes). Para que el fps pedido se cumpla pase lo que pase con la
        # luz, se limita ExposureAutoMax al periodo de frame (con un 10% de
        # margen) y se deja que GainAuto compense el brillo que falte
        # subiendo la ganancia en vez de alargando la exposición.
        #
        # Orden importante: la exposición ACTUAL puede venir larga de una
        # sesión anterior (p.ej. 45ms) y seguiría siéndolo un instante
        # después de solo cambiar ExposureAutoMax — eso hace fallar el
        # AcquisitionFrameRate.set() de más abajo (rango calculado con la
        # exposición vieja). Por eso primero se fuerza manualmente una
        # exposición corta, LUEGO se fija el fps, y solo entonces se vuelve
        # a automático con el techo ya puesto.
        max_exposure_us = 0.9 * 1e6 / target_fps
        try:
            cam.ExposureAuto.set("Off")
            elo, ehi = cam.ExposureTime.get_range()
            cam.ExposureTime.set(min(max(max_exposure_us, elo), ehi))
        except Exception:
            pass

    if exposure_us is None:
        try:
            cam.ExposureAuto.set("Continuous")
        except Exception:
            pass

        if guarantee_fps and target_fps:
            try:
                elo, ehi = cam.ExposureAutoMax.get_range()
                cam.ExposureAutoMax.set(min(max(max_exposure_us, elo), ehi))
            except Exception:
                pass
            try:
                cam.GainAuto.set("Continuous")
            except Exception:
                pass
    else:
        try:
            cam.ExposureAuto.set("Off")
        except (AttributeError, Exception):
            pass

        try:
            lo, hi = cam.ExposureTime.get_range()
            cam.ExposureTime.set(min(max(exposure_us, lo), hi))
        except Exception:
            pass

    if exposure_us is None and target_fps is None:
        # No imponer un techo de fps: si se fuerza AcquisitionFrameRate al
        # máximo actual, se limita el periodo de frame y con él la exposición
        # máxima que la auto-exposición puede llegar a usar — una cámara en
        # una zona oscura se queda atascada sin poder aclarar la imagen.
        # Se deja correr libre, a lo que la propia auto-exposición decida.
        try:
            cam.AcquisitionFrameRateEnable.set(False)
        except Exception:
            pass
        try:
            return cam.AcquisitionFrameRate.get()
        except Exception:
            return 0.0

    try:
        cam.AcquisitionFrameRateEnable.set(True)
    except Exception:
        pass

    if target_fps is not None:
        # Se fija el fps al valor pedido (no al máximo). Si la exposición
        # sigue en automático, la auto-exposición usará como máximo el
        # periodo de frame resultante para exponer lo mejor posible dentro
        # de ese límite — es justo lo que se quiere al comprobar cómo se ve
        # a un fps concreto con la luz actual. Se intenta fijar el valor
        # directo primero (evita que un ExposureTime "viejo" de una sesión
        # previa deje el rango de fps consultado artificialmente bajo).
        try:
            cam.AcquisitionFrameRate.set(target_fps)
        except Exception:
            try:
                fmin, fmax = cam.AcquisitionFrameRate.get_range()
                cam.AcquisitionFrameRate.set(min(max(target_fps, fmin), fmax))
            except Exception:
                pass
        try:
            return cam.AcquisitionFrameRate.get()
        except Exception:
            return target_fps

    fmin, fmax = cam.AcquisitionFrameRate.get_range()
    cam.AcquisitionFrameRate.set(fmax)
    return fmax


def run_all_staggered(cams: List[Camera], duration_s: float, exposure_us: Optional[float],
                       binning: int, stagger_s: float,
                       throughput_bps: Optional[float] = None,
                       ram_safety_fraction: float = 0.6,
                       target_fps: Optional[float] = None,
                       guarantee_fps: bool = False,
                       resolution: Optional["tuple[int, int]"] = None) -> List[CamResult]:
    """Abre y configura las 4 cámaras, y arranca su streaming una a una con
    `stagger_s` de separación (no todas a la vez). El envío de frames es
    asíncrono vía callback interno de VmbC, así que no hace falta un hilo por
    cámara: basta con esperar `duration_s` una vez arrancada la última.

    Mientras espera, vigila la RAM: los frames se acumulan sin comprimir en
    memoria (BayerRG8 sin codificar), así que a velocidad de fábrica 4
    cámaras pueden generar >1 GB/s. Si el total acumulado se acerca a
    `ram_safety_fraction` de la RAM disponible al arrancar, corta la captura
    antes de que el sistema entre en swap (compartido con otros usuarios de
    esta máquina)."""
    results = [CamResult(cam_id=c.get_id(), cam_name=c.get_name()) for c in cams]

    window_start_by_id = {}
    ram_budget = int(available_ram_bytes() * ram_safety_fraction)
    stopped_early_for_ram = False

    with ExitStack() as stack:
        opened = []

        for cam, res in zip(cams, results):
            try:
                stack.enter_context(cam)
                res.configured_fps = configure_camera(cam, exposure_us, binning,
                                                        throughput_bps, target_fps,
                                                        guarantee_fps, resolution)
                res.width = cam.Width.get()
                res.height = cam.Height.get()

                handler = RawCollector(res)
                # Buffer generoso: a >100fps y 15s sobra margen para absorber jitter
                cam.start_streaming(handler=handler, buffer_count=64)

                opened.append(cam)
                window_start_by_id[res.cam_id] = time.perf_counter()
            except Exception as e:
                res.error = str(e)
                continue

            if stagger_s > 0:
                time.sleep(stagger_s)

        deadline = time.perf_counter() + duration_s
        poll_s = 0.25
        while time.perf_counter() < deadline:
            time.sleep(poll_s)
            used = sum(len(r.frames) * r.width * r.height for r in results)
            if used >= ram_budget:
                stopped_early_for_ram = True
                break
        window_end = time.perf_counter()

        for cam in opened:
            try:
                cam.stop_streaming()
            except Exception:
                pass  # ya se habrá registrado en el frame filtering de abajo

    if stopped_early_for_ram:
        actual_s = window_end - min(window_start_by_id.values())
        print(f"!! Corte de seguridad por RAM: se alcanzó el {ram_safety_fraction*100:.0f}% "
              f"de la memoria disponible ({ram_budget / 1e9:.1f} GB) tras "
              f"~{actual_s:.1f}s en vez de los {duration_s:.0f}s pedidos.\n")

    # Descarta los frames llegados durante el setup/arranque escalonado:
    # solo cuentan los capturados dentro de la ventana de cada cámara.
    for res in results:
        w_start = window_start_by_id.get(res.cam_id)
        if w_start is None:
            continue
        kept_frames, kept_ts = [], []
        for f, t in zip(res.frames, res.timestamps):
            if w_start <= t <= window_end:
                kept_frames.append(f)
                kept_ts.append(t)
        res.frames, res.timestamps = kept_frames, kept_ts

    return results


def achieved_fps_of(result: CamResult) -> float:
    if len(result.timestamps) < 2:
        return 0.0
    elapsed = result.timestamps[-1] - result.timestamps[0]
    return (len(result.timestamps) - 1) / elapsed if elapsed > 0 else 0.0


def encode_mp4(result: CamResult, out_dir: Path, prefix: str = "speedtest") -> Optional[Path]:
    if not result.frames:
        return None

    # fps entero: mpeg4/ffmpeg rechaza framerates con demasiada precisión
    # decimal (denominador de timebase > 65535).
    fps_for_video = max(1, round(achieved_fps_of(result)))

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{prefix}_{result.cam_id}_{timestamp}.mp4"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps_for_video,
                              (result.width, result.height))
    try:
        for raw in result.frames:
            bgr = cv2.cvtColor(raw, BAYER_TO_BGR)
            writer.write(bgr)
    finally:
        writer.release()

    return out_path


def parse_resolution(s: str) -> "tuple[int, int]":
    try:
        w_str, h_str = s.lower().split("x")
        return (int(w_str), int(h_str))
    except Exception:
        raise argparse.ArgumentTypeError(
            f"'{s}' no es una resolución válida, usa el formato ANCHOxALTO "
            f"(p.ej. 1920x1080)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--duration", type=float, default=15.0,
                         help="duración de la captura en segundos (default 15)")
    parser.add_argument("-e", "--exposure", type=float, default=DEFAULT_EXPOSURE_US,
                         help="tiempo de exposición fijo en microsegundos (default 4000)")
    parser.add_argument("--auto-exposure", action="store_true",
                         help="deja la auto-exposición de cada cámara activa en vez de "
                              "fijar un valor manual (usar cuando las cámaras no reciben "
                              "la misma luz y algunas salen negras con exposición fija)")
    parser.add_argument("-b", "--binning", type=int, default=1,
                         help="binning horizontal/vertical (1=resolución completa, 2=mitad, ...)")
    parser.add_argument("--resolution", type=parse_resolution, default=None,
                         help="resolución ANCHOxALTO (p.ej. 1920x1080). Por defecto, "
                              "la máxima del sensor (tras aplicar el binning). Un "
                              "valor menor recorta el sensor centrado (p.ej. "
                              "1936x862 para alcanzar los 176fps reales de la "
                              "cámara, ver README).")
    parser.add_argument("--stagger", type=float, default=DEFAULT_STAGGER_S,
                         help="segundos entre el arranque de cada cámara (default 0.3; "
                              "0 = todas a la vez, reproduce las caídas por el bus)")
    parser.add_argument("--throughput", type=float, default=None,
                         help="DeviceLinkThroughputLimit en bytes/s por cámara "
                              "(default: no se toca, queda el de la cámara). "
                              "Usa un valor alto (p.ej. 450000000) para ir a "
                              "velocidad máxima de fábrica.")
    parser.add_argument("--ram-fraction", type=float, default=0.6,
                         help="fracción de la RAM disponible que se puede llenar "
                              "de frames en crudo antes de cortar la captura "
                              "por seguridad (default 0.6)")
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures" / "speedtest",
                         help="carpeta de salida para los .mp4")
    parser.add_argument("--target-fps", type=float, default=None,
                         help="fija el fps a este valor en vez de ir al máximo. "
                              "Con --auto-exposure, la auto-exposición se adapta "
                              "usando como máximo el periodo de frame resultante "
                              "(útil para comprobar cómo se ve a un fps concreto "
                              "con la luz actual).")
    parser.add_argument("--prefix", type=str, default="speedtest",
                         help="prefijo de los ficheros .mp4 generados (default 'speedtest')")
    parser.add_argument("--guarantee-fps", action="store_true",
                         help="con --target-fps y --auto-exposure: fuerza a cumplir el "
                              "fps pedido pase lo que pase con la luz, limitando "
                              "ExposureAutoMax al periodo de frame y dejando que "
                              "GainAuto compense el brillo que falte con ganancia "
                              "en vez de con más tiempo de exposición (imagen más "
                              "ruidosa en poca luz, pero fps real = fps pedido).")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    with VmbSystem.get_instance() as vmb:
        cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
        if not cams:
            print("No se ha encontrado ninguna cámara física.", file=sys.stderr)
            return 1

        print(f"Prueba de velocidad: {len(cams)} cámara(s), {args.duration:.0f}s, "
              f"formato {PIXEL_FORMAT.name}, arranque escalonado cada "
              f"{args.stagger * 1000:.0f}ms\n")

        exposure = None if args.auto_exposure else args.exposure

        t0 = time.perf_counter()
        results = run_all_staggered(cams, args.duration, exposure,
                                     args.binning, args.stagger,
                                     args.throughput, args.ram_fraction,
                                     args.target_fps, args.guarantee_fps,
                                     args.resolution)
        wall_duration = time.perf_counter() - t0

        print(f"Captura finalizada en {wall_duration:.2f}s de pared.\n")

        # --- Informe de throughput ---
        header = (f"{'Cámara':<20}{'fps cfg':>9}{'frames':>9}{'incompl.':>10}"
                  f"{'fps real':>10}{'MB/s':>9}")
        print(header)
        print("-" * len(header))

        total_mbps = 0.0
        for res in results:
            n = len(res.frames)
            fps_real = achieved_fps_of(res)
            bytes_per_frame = res.width * res.height  # BayerRG8 = 1 byte/px
            mbps = fps_real * bytes_per_frame / 1e6

            print(f"{res.cam_id:<20}{res.configured_fps:>9.2f}{n:>9}"
                  f"{res.incomplete:>10}{fps_real:>10.2f}{mbps:>9.1f}")
            if res.error:
                print(f"{'':<20} !! incidencia durante la captura: {res.error}")
            total_mbps += mbps

        print("-" * len(header))
        print(f"{'TOTAL':<20}{'':>9}{'':>9}{'':>10}{'':>10}{total_mbps:>9.1f}  MB/s agregados\n")

        # --- Codificación a MP4 ---
        print("Escribiendo vídeos .mp4 ...")
        for res in results:
            if not res.frames:
                print(f"  {res.cam_id:<20} -> (sin frames, no se genera vídeo)")
                continue
            path = encode_mp4(res, args.output, args.prefix)
            print(f"  {res.cam_id:<20} -> {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
