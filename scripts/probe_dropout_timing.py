#!/usr/bin/env python
"""Diagnóstico: ¿el fallo de ancho de banda del hub aparece siempre muy pronto
(techo duro instantáneo) o depende de cuánto dure la captura (degradación
acumulativa / contención probabilística)?

Lanza varias repeticiones con duraciones distintas y, para cada cámara,
registra el instante (relativo al arranque común de la tanda) del último
frame recibido antes de fallar, o de fin de ventana si no falla.

No guarda ningún vídeo: es solo para medir el patrón de caída del bus.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from vmbpy import Camera, Frame, FrameStatus, Stream, VmbSystem

from speed_test_capture import PIXEL_FORMAT, configure_camera

EXPOSURE_US = 4000.0


@dataclass
class ProbeResult:
    cam_id: str
    frame_count: int = 0
    last_ts: Optional[float] = None
    error: Optional[str] = None


class TimestampOnly:
    """Handler ligero: no copia el frame, solo apunta cuándo llegó."""

    def __init__(self, result: ProbeResult):
        self.result = result

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            self.result.frame_count += 1
            self.result.last_ts = time.perf_counter()
        cam.queue_frame(frame)


def run_camera(cam: Camera, duration_s: float, result: ProbeResult, binning: int = 1) -> None:
    try:
        with cam:
            configure_camera(cam, EXPOSURE_US, binning)
            handler = TimestampOnly(result)
            cam.start_streaming(handler=handler, buffer_count=64)
            time.sleep(duration_s)
            cam.stop_streaming()
    except Exception as e:
        result.error = str(e)


def one_repetition(cams: List[Camera], duration_s: float, rep_label: str,
                    binning: int = 1) -> None:
    results = [ProbeResult(cam_id=c.get_id()) for c in cams]
    threads = [threading.Thread(target=run_camera, args=(cam, duration_s, res, binning))
               for cam, res in zip(cams, results)]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n--- {rep_label} (duración pedida: {duration_s:.0f}s) ---")
    print(f"{'Cámara':<20}{'frames':>8}{'último frame (s desde t0)':>28}{'estado':>12}")
    for res in results:
        rel = f"{res.last_ts - t0:.2f}" if res.last_ts is not None else "-"
        estado = "CAIDA" if res.error else "OK"
        print(f"{res.cam_id:<20}{res.frame_count:>8}{rel:>28}{estado:>12}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--binning", type=int, default=1)
    args = parser.parse_args()

    with VmbSystem.get_instance() as vmb:
        cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
        if len(cams) < 2:
            print("Necesito al menos 2 cámaras físicas para esta prueba.")
            return

        plan = [
            ("Rep 1", 6.0),
            ("Rep 2", 6.0),
            ("Rep 3", 15.0),
            ("Rep 4", 15.0),
        ]
        for label, duration in plan:
            one_repetition(cams, duration, label, args.binning)
            time.sleep(2.0)  # deja que el bus se asiente entre repeticiones


if __name__ == "__main__":
    main()
