#!/usr/bin/env python
"""Captura sincronizada de las 4 cámaras por trigger de hardware desde el
CC320 (vía Line0), con fps y resolución configurables.

Este script siempre LEE primero el periodo de trigger ya programado en el
CC320 (comando de solo lectura `ST`). Con ese dato:
  - si no se pide --fps, lo deja como está y lo usa para calcular una
    exposición segura por defecto (si no se indica -e);
  - si se pide --fps y ya coincide, tampoco toca nada;
  - si se pide --fps y NO coincide, lo programa (comando `RB1,p`, un único
    envío, sin guardarlo de forma permanente con AW) y vuelve a leer para
    confirmar el cambio antes de seguir.

Al capturar, también abre/cierra la puerta del CC320 (RV3,1 / RV3,0 — un
canal de salida usado como interruptor por software) con el mínimo de
comandos posible: un único RV3,1 al empezar, un único RV3,0 al terminar.

Uso:
    python hw_trigger_capture.py -d SEGUNDOS [--fps FPS] [--resolution WxH]
"""
import argparse
import os
import re
import socket
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Optional

import cv2
from vmbpy import Camera, Frame, FrameStatus, PixelFormat, Stream, VmbSystem

from speed_test_capture import parse_resolution, set_resolution

CC320_PORT = 30313
STAGGER_S = 0.3
# Verificado contra la conversión BGR8 nativa del SDK: con BayerRG2BGR los
# canales rojo y azul salían intercambiados.
BAYER_TO_BGR = cv2.COLOR_BayerBG2BGR


def cc320(cmd: str, ip: str, connect_timeout: float = 3.0, settle: float = 0.3) -> str:
    """Envía un comando y lee la respuesta completa hasta el terminador '>'
    (en vez de una sola lectura de tamaño fijo) — necesario para respuestas
    largas como la de ST, que no caben en un solo paquete."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(connect_timeout)
    s.connect((ip, CC320_PORT))
    s.sendall(cmd.encode() + b"\r")
    time.sleep(settle)
    s.settimeout(0.5)
    chunks = []
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b">" in chunk:
                break
    except socket.timeout:
        pass
    s.close()
    return b"".join(chunks).decode(errors="replace")


def read_trigger_period_ms(ip: str) -> float:
    """Lee el periodo de trigger actualmente programado en el CC320 (comando
    de solo lectura ST). No cambia nada en el dispositivo.

    El CC320 reporta el periodo en la unidad que corresponda según su
    magnitud (s, ms o us — ver manual CC320 sección 10.2/10.3, comando ST),
    no siempre en ms."""
    reply = cc320("ST", ip)
    m = re.search(r"trigger period\s*=\s*([\d.]+)\s*(ms|us|s)?", reply, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"no se pudo interpretar la respuesta de ST: {reply!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    if unit == "s":
        return value * 1000.0
    if unit == "us":
        return value / 1000.0
    return value


def set_trigger_period_ms(ip: str, period_ms: float, settle: float = 0.5) -> str:
    """Programa el periodo del trigger interno del CC320 (comando RB1,p —
    manual CC320 sección 10.3, "Set the internal free running trigger").
    Un único comando, sin reintentos: enviar muchos comandos de
    reconfiguración seguidos ya bloqueó el CC320 una vez en este proyecto.

    No se envía AW: el cambio no se guarda de forma permanente, así que un
    corte de corriente del CC320 restaura el último periodo que sí se
    guardó (desde su web/teclado, o con AW)."""
    return cc320(f"RB1,{period_ms:.1f}ms", ip, settle=settle)


def available_ram_bytes() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2 * 1024 ** 3


class Collector:
    def __init__(self):
        self.frames = []

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            self.frames.append((time.perf_counter(), frame.as_numpy_ndarray().copy()))
        cam.queue_frame(frame)


def configure(cam: Camera, exposure_us: float, resolution: Optional["tuple[int, int]"]) -> None:
    cam.set_pixel_format(PixelFormat.BayerRG8)
    # Si una sesión anterior dejó la cámara en modo libre con
    # AcquisitionFrameRateEnable=True, TriggerMode.set("On") más abajo
    # falla con "write access not allowed": son modos mutuamente excluyentes.
    try:
        cam.AcquisitionFrameRateEnable.set(False)
    except Exception:
        pass
    set_resolution(cam, resolution)
    tlo, thi = cam.DeviceLinkThroughputLimit.get_range()
    cam.DeviceLinkThroughputLimit.set(thi)
    cam.ExposureAuto.set("Off")
    lo, hi = cam.ExposureTime.get_range()
    cam.ExposureTime.set(min(max(exposure_us, lo), hi))
    cam.TriggerSelector.set("FrameStart")
    cam.TriggerSource.set("Line0")
    cam.TriggerActivation.set("RisingEdge")
    cam.TriggerMode.set("On")


EPILOG = """\
Ejemplos:
  # 5s, usando el fps y la resolución que ya tenga el CC320/la cámara
  python hw_trigger_capture.py -d 5

  # 5s a 60fps: si el CC320 no está ya a 60fps, lo reprograma antes de capturar
  python hw_trigger_capture.py -d 5 --fps 60

  # 2.5s, recorte 1K (más fps posibles que a resolución completa)
  python hw_trigger_capture.py -d 2.5 --resolution 1920x1080

  # el recorte con el que esta cámara alcanza su techo real (176fps)
  python hw_trigger_capture.py -d 2 --fps 176 --resolution 1936x862

Resoluciones de referencia de esta cámara (Alvium 1800 U-240c), ANCHOxALTO:
  1936x1216   máxima del sensor (valor por defecto, sin --resolution)
  1920x1080   1080p/1K estándar
  1936x862    recorte con el que se alcanzan los 176fps reales de la cámara
  968x608     mitad de la máxima (menos detalle, más margen de fps/ancho de banda)

--resolution acepta cualquier ANCHOxALTO dentro del rango del sensor (no
hace falta que sea uno de los de arriba): se recorta centrado al valor
más cercano permitido por los incrementos de la cámara, nunca reescala.
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-d", "--duration", type=float, required=True,
                         help="duración de la captura en segundos. "
                              "Obligatorio, sin valor por defecto. "
                              "Ejemplo: -d 5  (5 segundos)")
    parser.add_argument("--fps", type=float, default=None,
                         help="fps deseado (p.ej. --fps 60). Si no coincide "
                              "con el periodo ya programado en el CC320, lo "
                              "reprograma (un único comando, ver manual CC320 "
                              "sección 10.3) antes de capturar. Opcional: si "
                              "no se indica, deja el fps que ya esté "
                              "programado en el CC320 tal cual.")
    parser.add_argument("--resolution", type=parse_resolution, default=None,
                         help="resolución ANCHOxALTO (p.ej. --resolution "
                              "1920x1080). Opcional: por defecto, la máxima "
                              "del sensor (1936x1216 en esta cámara). Un "
                              "valor menor recorta centrado, no reescala — "
                              "ver más abajo valores de referencia conocidos "
                              "de esta cámara.")
    parser.add_argument("-e", "--exposure", type=float, default=None,
                         help="exposición fija en microsegundos. Por defecto, "
                              "90%% del periodo de trigger ya programado en "
                              "el CC320 (margen de seguridad para no perder "
                              "disparos).")
    parser.add_argument("--ram-fraction", type=float, default=0.6,
                         help="fracción de la RAM disponible que se puede "
                              "llenar de frames en crudo antes de cortar la "
                              "captura por seguridad (default 0.6)")
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures" / "hwtrigger",
                         help="carpeta de salida para los .mp4 (default "
                              "captures/hwtrigger)")
    parser.add_argument("--prefix", type=str, default="hwtrig",
                         help="prefijo de los ficheros .mp4 generados "
                              "(default 'hwtrig')")
    parser.add_argument("--cc320-ip", default=os.environ.get("PREMOV_CC320_IP"),
                         help="IP del controlador CC320 (o variable de entorno "
                              "PREMOV_CC320_IP; ver .env.example)")
    args = parser.parse_args()

    if not args.cc320_ip:
        print("Falta la IP del CC320: pasa --cc320-ip o define PREMOV_CC320_IP "
              "(ver .env.example).", file=sys.stderr)
        return 1

    try:
        period_ms = read_trigger_period_ms(args.cc320_ip)
    except Exception as e:
        print(f"No se pudo leer el estado del CC320 ({type(e).__name__}): {e}",
              file=sys.stderr)
        return 1

    configured_fps = 1000.0 / period_ms

    if args.fps is not None and abs(configured_fps - args.fps) / args.fps > 0.01:
        target_period_ms = round(1000.0 / args.fps, 1)
        print(f"El CC320 está a {configured_fps:.2f}fps (periodo {period_ms:.3f}ms); "
              f"programando {args.fps:.2f}fps pedidos (periodo {target_period_ms:.1f}ms)...")
        try:
            set_trigger_period_ms(args.cc320_ip, target_period_ms)
            period_ms = read_trigger_period_ms(args.cc320_ip)
        except Exception as e:
            print(f"No se pudo programar/verificar el periodo del CC320 "
                  f"({type(e).__name__}): {e}", file=sys.stderr)
            return 1

        configured_fps = 1000.0 / period_ms
        if abs(configured_fps - args.fps) / args.fps > 0.01:
            print(f"El CC320 sigue sin coincidir tras programarlo: ahora está "
                  f"a {configured_fps:.2f}fps (periodo {period_ms:.3f}ms), no "
                  f"a los {args.fps:.2f}fps pedidos.", file=sys.stderr)
            return 1
        print(f"CC320 reprogramado a {configured_fps:.2f}fps "
              f"(periodo {period_ms:.3f}ms).")

    exposure_us = args.exposure if args.exposure is not None else 0.9 * period_ms * 1000.0
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"CC320: periodo programado {period_ms:.3f}ms ({configured_fps:.2f}fps), "
          f"exposición {exposure_us:.0f}us")

    try:
        with VmbSystem.get_instance() as vmb:
            cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
            if not cams:
                print("No hay cámaras físicas.", file=sys.stderr)
                return 1

            results = {c.get_id(): Collector() for c in cams}
            dims = {}
            ram_budget = int(available_ram_bytes() * args.ram_fraction)
            stopped_early = False

            with ExitStack() as stack:
                opened = []
                for cam in cams:
                    stack.enter_context(cam)
                    configure(cam, exposure_us, args.resolution)
                    dims[cam.get_id()] = (cam.Width.get(), cam.Height.get())
                    handler = results[cam.get_id()]
                    cam.start_streaming(handler=handler, buffer_count=64)
                    opened.append(cam)
                    w, h = dims[cam.get_id()]
                    print(f"{cam.get_id()}: armada (TriggerMode=On, Line0, "
                          f"{w}x{h}, exposición {exposure_us:.0f}us)")
                    time.sleep(STAGGER_S)

                t_ready = time.perf_counter()
                print(f"\nLas {len(opened)} cámaras armadas. Abriendo el gate (RV3,1)...")
                print(" ", cc320("RV3,1", args.cc320_ip))

                deadline = t_ready + args.duration
                while time.perf_counter() < deadline:
                    time.sleep(0.1)
                    used = sum(len(r.frames) * dims[cid][0] * dims[cid][1]
                               for cid, r in results.items())
                    if used >= ram_budget:
                        stopped_early = True
                        break

                print("Cerrando el gate (RV3,0)...")
                print(" ", cc320("RV3,0", args.cc320_ip))
                elapsed = time.perf_counter() - t_ready

                for cam in opened:
                    cam.stop_streaming()
                    try:
                        # Deja la cámara en modo libre: si no,
                        # AcquisitionFrameRate queda de solo lectura para
                        # cualquier script posterior que no use trigger.
                        cam.TriggerMode.set("Off")
                    except Exception:
                        pass
    except Exception as e:
        print(f"Error durante la captura ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    if stopped_early:
        print(f"\n!! Corte de seguridad por RAM tras {elapsed:.2f}s "
              f"(pedidos {args.duration:.1f}s)")

    for res in results.values():
        res.frames = [(t, f) for t, f in res.frames if t >= t_ready]

    print(f"\n--- Resultado tras {elapsed:.2f}s a {configured_fps:.2f}fps "
          f"programados (esperado ~{elapsed * configured_fps:.0f} frames/cámara) ---")
    for cid, res in results.items():
        n = len(res.frames)
        if n >= 2:
            span = res.frames[-1][0] - res.frames[0][0]
            fps_real = (n - 1) / span if span > 0 else 0
            print(f"{cid}: {n} frames  fps_real={fps_real:.2f}")
        else:
            print(f"{cid}: {n} frames")

    primeros = [res.frames[0][0] for res in results.values() if res.frames]
    if len(primeros) > 1:
        print(f"\nDesalineación entre el primer frame de cada cámara: "
              f"{(max(primeros) - min(primeros)) * 1000:.2f} ms")

    print("\nEscribiendo .mp4 ...")
    for cid, res in results.items():
        if not res.frames:
            continue
        w, h = dims[cid]
        span = res.frames[-1][0] - res.frames[0][0]
        fps_video = max(1, round((len(res.frames) - 1) / span)) if span > 0 else 1
        out_path = args.output / f"{args.prefix}_{cid}.mp4"
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                  fps_video, (w, h))
        for _, raw in res.frames:
            writer.write(cv2.cvtColor(raw, BAYER_TO_BGR))
        writer.release()
        print(f"  {cid} -> {out_path}  ({len(res.frames)} frames @ {fps_video}fps)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
