#!/usr/bin/env python
"""Diagnóstico: ¿el fallo del bus es por el ancho de banda sostenido, o por
abrir los 4 canales de streaming USB3 Vision exactamente a la vez?

En vez de lanzar `start_streaming()` en las 4 cámaras simultáneamente (como
hacían speed_test_capture.py / probe_dropout_timing.py), aquí se configuran
las 4 primero y luego se arrancan una a una con un pequeño retardo entre
cada `start_streaming()`. Si así desaparecen las caídas, el problema es la
negociación de conexión simultánea, no el caudal de datos en régimen
estable — algo a tener en cuenta si más adelante se necesita arrancar todas
a la vez para captura sincronizada.

No graba vídeo: solo mide supervivencia y throughput.
"""
import argparse
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import List, Optional

from vmbpy import Camera, Frame, FrameStatus, Stream, VmbSystem

from speed_test_capture import PIXEL_FORMAT, configure_camera

EXPOSURE_US = 4000.0


@dataclass
class ProbeResult:
    cam_id: str
    frame_count: int = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None


class TimestampOnly:
    def __init__(self, result: ProbeResult):
        self.result = result

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            now = time.perf_counter()
            if self.result.first_ts is None:
                self.result.first_ts = now
            self.result.last_ts = now
            self.result.frame_count += 1
        cam.queue_frame(frame)


def one_repetition(cams: List[Camera], duration_s: float, stagger_s: float,
                    binning: int, rep_label: str) -> None:
    with ExitStack() as stack:
        opened = [stack.enter_context(cam) for cam in cams]

        results, handlers = [], []
        for cam in opened:
            configure_camera(cam, EXPOSURE_US, binning)
            res = ProbeResult(cam_id=cam.get_id())
            results.append(res)
            handlers.append(TimestampOnly(res))

        t0 = time.perf_counter()
        start_offsets = []
        for cam, handler in zip(opened, handlers):
            cam.start_streaming(handler=handler, buffer_count=64)
            start_offsets.append(time.perf_counter() - t0)
            if stagger_s > 0:
                time.sleep(stagger_s)

        time.sleep(duration_s)

        for cam in opened:
            try:
                cam.stop_streaming()
            except Exception:
                pass

    print(f"\n--- {rep_label} (stagger={stagger_s*1000:.0f}ms, "
          f"duración post-arranque={duration_s:.0f}s, binning={binning}) ---")
    print(f"{'Cámara':<20}{'arrancó a (s)':>14}{'frames':>8}{'estado':>10}")
    for res, offset in zip(results, start_offsets):
        estado = "OK" if res.frame_count > 0 else "CAIDA"
        print(f"{res.cam_id:<20}{offset:>14.2f}{res.frame_count:>8}{estado:>10}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--stagger-ms", type=float, default=300.0,
                         help="retardo entre el arranque de cada cámara, en ms")
    parser.add_argument("-d", "--duration", type=float, default=6.0,
                         help="segundos de streaming tras arrancar la última cámara")
    parser.add_argument("-b", "--binning", type=int, default=1)
    parser.add_argument("-n", "--reps", type=int, default=4)
    args = parser.parse_args()

    with VmbSystem.get_instance() as vmb:
        cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
        if len(cams) < 2:
            print("Necesito al menos 2 cámaras físicas para esta prueba.")
            return

        for i in range(args.reps):
            one_repetition(cams, args.duration, args.stagger_ms / 1000.0,
                            args.binning, f"Rep {i + 1}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
