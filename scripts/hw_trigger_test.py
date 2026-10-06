#!/usr/bin/env python
"""Prueba de captura disparada por el CC320 (trigger hardware por Line0).

Configura la cámara en modo TriggerMode=On / TriggerSource=Line0, abre la
puerta del CC320 (arranca los pulsos), captura durante `--duration` segundos,
cierra la puerta y guarda cada frame recibido como PNG (con timestamp propio
de llegada) para poder inspeccionarlos visualmente.

Uso:
    python hw_trigger_test.py [-c CAMERA_ID] [-d SEGUNDOS] [-o DIR]
"""
import argparse
import os
import socket
import sys
import time
from pathlib import Path

import cv2
from vmbpy import Camera, Frame, FrameStatus, PixelFormat, Stream, VmbSystem

CC320_PORT = 30313
BAYER_TO_BGR = cv2.COLOR_BayerRG2BGR


def cc320(cmd: str, ip: str, wait: float = 0.3) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect((ip, CC320_PORT))
    s.sendall(cmd.encode() + b"\r")
    time.sleep(wait)
    reply = s.recv(2048).decode(errors="replace")
    s.close()
    return reply.strip()


class TriggeredCollector:
    def __init__(self):
        self.frames = []  # (t_llegada, ndarray)

    def __call__(self, cam: Camera, stream: Stream, frame: Frame):
        if frame.get_status() == FrameStatus.Complete:
            self.frames.append((time.perf_counter(), frame.as_numpy_ndarray().copy()))
        cam.queue_frame(frame)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--camera", default="DEV_1AB22C0C4403",
                         help="ID de la cámara (default DEV_1AB22C0C4403, serie 0H88Z)")
    parser.add_argument("-d", "--duration", type=float, default=30.0,
                         help="duración de la captura en segundos (default 30)")
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures" / "hwtrigger",
                         help="carpeta de salida para los PNG")
    parser.add_argument("-e", "--exposure", type=float, default=20000.0,
                         help="exposición fija en microsegundos (default 20000 = 20ms). "
                              "Con periodos de trigger largos, la auto-exposición sin techo "
                              "puede crecer por encima del periodo y perder disparos.")
    parser.add_argument("--cc320-ip", default=os.environ.get("PREMOV_CC320_IP"),
                         help="IP del controlador CC320 (o variable de entorno "
                              "PREMOV_CC320_IP; ver .env.example)")
    args = parser.parse_args()
    if not args.cc320_ip:
        print("Falta la IP del CC320: pasa --cc320-ip o define PREMOV_CC320_IP "
              "(ver .env.example).", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)

    with VmbSystem.get_instance() as vmb:
        cam = vmb.get_camera_by_id(args.camera)
        with cam:
            cam.set_pixel_format(PixelFormat.BayerRG8)
            cam.ExposureAuto.set("Off")
            lo, hi = cam.ExposureTime.get_range()
            cam.ExposureTime.set(min(max(args.exposure, lo), hi))
            try:
                cam.AcquisitionFrameRateEnable.set(False)
            except Exception:
                pass  # ya bloqueado a solo lectura si TriggerMode=On viene de antes
            cam.TriggerSelector.set("FrameStart")
            cam.TriggerSource.set("Line0")
            cam.TriggerActivation.set("RisingEdge")
            cam.TriggerMode.set("On")

            handler = TriggeredCollector()
            cam.start_streaming(handler=handler, buffer_count=32)

            print(f"Prueba de trigger externo: {args.camera}, {args.duration:.0f}s")
            print(cc320("RV3,1", args.cc320_ip))  # abre la puerta: arrancan los pulsos del CC320
            t_start = time.perf_counter()
            time.sleep(args.duration)
            print(cc320("RV3,0", args.cc320_ip))  # cierra la puerta
            elapsed = time.perf_counter() - t_start

            cam.stop_streaming()
            try:
                # Deja la cámara en modo libre: si no, AcquisitionFrameRate
                # queda de solo lectura para cualquier script posterior
                # que no use trigger (p.ej. speed_test_capture.py).
                cam.TriggerMode.set("Off")
            except Exception:
                pass

        n = len(handler.frames)
        print(f"\nFrames recibidos: {n}  en {elapsed:.2f}s  "
              f"(fps efectivo: {n / elapsed:.3f})")

        for i, (t, raw) in enumerate(handler.frames):
            bgr = cv2.cvtColor(raw, BAYER_TO_BGR)
            out_path = args.output / f"hwtrig_{args.camera}_{i:02d}.png"
            cv2.imwrite(str(out_path), bgr)
            print(f"  [{i:02d}] t={t - t_start:6.2f}s -> {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
