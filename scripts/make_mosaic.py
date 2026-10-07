#!/usr/bin/env python
"""Monta un mosaico 2x2 a partir de 4 vídeos (p.ej. las 4 cámaras de una
captura sincronizada), para comprobar visualmente que van a la par.

Reduce cada vídeo al 50% de su tamaño original y los compone en un único
mosaico 2x2, además de guardar cada vídeo reducido por separado. Procesa
frame a frame (streaming), sin cargar los vídeos enteros en memoria.

Uso:
    python make_mosaic.py cam1.mp4 cam2.mp4 cam3.mp4 cam4.mp4 [-o DIR]
"""
import argparse
import sys
from pathlib import Path

import cv2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs=4, type=Path,
                         help="los 4 vídeos de entrada, en orden "
                              "arriba-izquierda, arriba-derecha, "
                              "abajo-izquierda, abajo-derecha")
    parser.add_argument("-o", "--output-dir", type=Path, default=None,
                         help="carpeta de salida (por defecto, la misma "
                              "carpeta del primer vídeo de entrada)")
    args = parser.parse_args()

    for v in args.videos:
        if not v.is_file():
            print(f"No existe el fichero: {v}", file=sys.stderr)
            return 1

    out_dir = args.output_dir or args.videos[0].parent
    out_dir.mkdir(parents=True, exist_ok=True)

    caps = [cv2.VideoCapture(str(v)) for v in args.videos]
    fps_list = []
    for v, cap in zip(args.videos, caps):
        fps = cap.get(cv2.CAP_PROP_FPS)
        fps_list.append(fps)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        print(f"{v.name}: {n} frames (fps={fps:.0f})")

    fps_out = max(1, round(sum(fps_list) / len(fps_list)))

    w = int(caps[0].get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(caps[0].get(cv2.CAP_PROP_FRAME_HEIGHT))
    half_w, half_h = w // 2, h // 2
    print(f"fps de salida: {fps_out}  tamaño original: {w}x{h}  mitad: {half_w}x{half_h}")

    half_writers = [
        cv2.VideoWriter(str(out_dir / f"half_{v.stem}.mp4"),
                         cv2.VideoWriter_fourcc(*"mp4v"), fps_out, (half_w, half_h))
        for v in args.videos
    ]
    mosaic_path = out_dir / "mosaic_2x2.mp4"
    mosaic_writer = cv2.VideoWriter(str(mosaic_path),
                                     cv2.VideoWriter_fourcc(*"mp4v"), fps_out,
                                     (half_w * 2, half_h * 2))

    count = 0
    try:
        while True:
            smalls = []
            ok_all = True
            for cap in caps:
                ok, frame = cap.read()
                if not ok:
                    ok_all = False
                    break
                smalls.append(cv2.resize(frame, (half_w, half_h), interpolation=cv2.INTER_AREA))
            if not ok_all:
                break

            for writer, small in zip(half_writers, smalls):
                writer.write(small)

            top = cv2.hconcat(smalls[0:2])
            bottom = cv2.hconcat(smalls[2:4])
            mosaic_writer.write(cv2.vconcat([top, bottom]))
            count += 1
    finally:
        for cap in caps:
            cap.release()
        for writer in half_writers:
            writer.release()
        mosaic_writer.release()

    print(f"\nFrames procesados (recorte al vídeo más corto): {count}")
    for v in args.videos:
        print(f"  {out_dir / f'half_{v.stem}.mp4'}")
    print(f"  {mosaic_path}  ({half_w * 2}x{half_h * 2} @ {fps_out}fps)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
