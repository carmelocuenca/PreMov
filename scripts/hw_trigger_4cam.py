#!/usr/bin/env python
"""Captura pasiva con las 4 cámaras a la vez, disparadas por el CC320 via Line0.

No manda NINGÚN comando al CC320 — se asume que ya está generando sus pulsos
(como se confirmó con la cámara 0H88Z). Solo configura el lado de la cámara
(TriggerMode=On, TriggerSource=Line0) y escucha durante `--duration` segundos,
con el arranque de streaming escalonado entre cámaras (necesario para evitar
las caídas de conexión USB3 Vision que ya conocemos, nada que ver con el
trigger en sí).

Uso:
    python hw_trigger_4cam.py [-d SEGUNDOS] [-o DIR]
"""
import argparse
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import cv2
from vmbpy import Camera, Frame, FrameStatus, PixelFormat, Stream, VmbSystem

STAGGER_S = 0.3


class Collector:
    def __init__(self):
        self.frames = []

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            self.frames.append((time.perf_counter(), frame.as_numpy_ndarray().copy()))
        cam.queue_frame(frame)


def configure(cam: Camera) -> None:
    """Solo toca la cámara. No manda nada al CC320."""
    cam.set_pixel_format(PixelFormat.BayerRG8)
    # Periodo de trigger generoso (2s) -> auto-exposición es segura aquí,
    # y da buena imagen en las 4 cámaras aunque tengan luz muy distinta.
    try:
        cam.ExposureAuto.set("Continuous")
    except Exception:
        pass
    cam.TriggerSelector.set("FrameStart")
    cam.TriggerSource.set("Line0")
    cam.TriggerActivation.set("RisingEdge")
    cam.TriggerMode.set("On")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--duration", type=float, default=30.0)
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures" / "hwtrigger")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    with VmbSystem.get_instance() as vmb:
        cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
        if not cams:
            print("No hay cámaras físicas.", file=sys.stderr)
            return 1

        results = {c.get_id(): Collector() for c in cams}
        errors = {}

        with ExitStack() as stack:
            opened = []
            for cam in cams:
                try:
                    stack.enter_context(cam)
                    configure(cam)
                    handler = results[cam.get_id()]
                    cam.start_streaming(handler=handler, buffer_count=32)
                    opened.append(cam)
                    print(f"{cam.get_id()}: streaming armado (TriggerMode=On, Line0)")
                except Exception as e:
                    errors[cam.get_id()] = str(e)
                    print(f"{cam.get_id()}: ERROR al armar -> {e}")
                time.sleep(STAGGER_S)

            # Instante en que las 4 cámaras ya están armadas: cualquier frame
            # anterior a esto se descarta (venía del desfase del arranque
            # escalonado). Como las 4 reciben el mismo pulso físico del
            # CC320, el primer frame retenido de cada una corresponde al
            # mismo disparo real -> quedan alineadas en el tiempo.
            t_ready = time.perf_counter()
            print(f"\nLas {len(opened)} cámaras armadas. Vaciando lo llegado antes de "
                  f"este instante y escuchando {args.duration:.0f}s más...")
            time.sleep(args.duration)
            elapsed = time.perf_counter() - t_ready

            for cam in opened:
                try:
                    cam.stop_streaming()
                except Exception:
                    pass

        # Descarta los frames que llegaron durante el arranque escalonado,
        # antes de que las 4 cámaras estuvieran listas.
        for res in results.values():
            kept = [(t, f) for t, f in res.frames if t >= t_ready]
            res.frames = kept
        t0 = t_ready

        print(f"\n--- Resultado tras {elapsed:.2f}s (frames anteriores al arranque comun descartados) ---")
        for cid, res in results.items():
            primero = f"{res.frames[0][0] - t0:.4f}s" if res.frames else "-"
            print(f"{cid}: {len(res.frames)} frames  (primero retenido a t={primero})")
            if cid in errors:
                print(f"   !! {errors[cid]}")

        primeros = [res.frames[0][0] for res in results.values() if res.frames]
        if len(primeros) > 1:
            spread_ms = (max(primeros) - min(primeros)) * 1000
            print(f"\nDesalineación entre el primer frame de cada cámara: {spread_ms:.2f} ms")

        print("\nGuardando una muestra (primer y último frame) por cámara...")
        for cid, res in results.items():
            if not res.frames:
                continue
            for tag, (t, raw) in [("primero", res.frames[0]), ("ultimo", res.frames[-1])]:
                bgr = cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR)
                path = args.output / f"hw4cam_{cid}_{tag}.png"
                cv2.imwrite(str(path), bgr)
                print(f"  {cid} [{tag}] t={t - t0:6.2f}s -> {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
