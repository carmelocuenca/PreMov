#!/usr/bin/env python
"""Captura un fotograma de cada cámara física conectada al hub USB y lo guarda como PNG.

Uso:
    python capture_frames.py [--all] [-o DIR]

    --all   Incluye también las cámaras simuladas de VimbaX (Cam1/Cam2/Cam3).
            Por defecto solo se capturan las cámaras físicas reales.
    -o DIR  Carpeta de salida (por defecto: ../captures respecto a este script).
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import cv2
from vmbpy import Camera, PixelFormat, VmbCameraError, VmbSystem

OPENCV_FORMAT = PixelFormat.Bgr8


def is_simulated(cam: Camera) -> bool:
    return "Simulator" in cam.get_name()


def setup_pixel_format(cam: Camera) -> None:
    """Configura la cámara para entregar frames en un formato convertible a BGR8."""
    cam_formats = cam.get_pixel_formats()

    if OPENCV_FORMAT in cam_formats:
        cam.set_pixel_format(OPENCV_FORMAT)
        return

    convertible = [f for f in cam_formats if OPENCV_FORMAT in f.get_convertible_formats()]
    if convertible:
        cam.set_pixel_format(convertible[0])
        return

    raise RuntimeError(f"'{cam.get_id()}' no soporta ningún formato convertible a BGR8")


def capture_one(cam: Camera, out_dir: Path, timeout_ms: int = 3000) -> Path:
    with cam:
        setup_pixel_format(cam)

        frame = cam.get_frame(timeout_ms=timeout_ms)

        if frame.get_pixel_format() != OPENCV_FORMAT:
            frame = frame.convert_pixel_format(OPENCV_FORMAT)

        image = frame.as_opencv_image()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{cam.get_id()}_{timestamp}.png"
    cv2.imwrite(str(out_path), image)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                         help="incluir también las cámaras simuladas")
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures",
                         help="carpeta de salida para los PNG")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    with VmbSystem.get_instance() as vmb:
        cams = vmb.get_all_cameras()

        if not args.all:
            cams = [c for c in cams if not is_simulated(c)]

        if not cams:
            print("No se ha encontrado ninguna cámara.", file=sys.stderr)
            return 1

        print(f"Capturando {len(cams)} cámara(s)...\n")

        errors = 0
        for cam in cams:
            try:
                path = capture_one(cam, args.output)
                print(f"  OK  {cam.get_id():<20} ({cam.get_name()}) -> {path}")
            except (VmbCameraError, RuntimeError) as e:
                errors += 1
                print(f"  FALLO {cam.get_id():<18} ({cam.get_name()}): {e}")

        print()
        if errors:
            print(f"{errors} cámara(s) con error.")
        return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
