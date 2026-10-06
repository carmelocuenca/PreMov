#!/usr/bin/env python
"""Captura sincronizada de las 4 cámaras a 125 fps, disparadas por hardware
desde el CC320 (temporizador a 8.000ms, gate=Output3 controlado por software).

No reconfigura el CC320 más que con el mínimo imprescindible (un único
RV3,1 al empezar, un único RV3,0 al terminar) — la configuración de los
canales (periodo, gate, pulso) se asume ya hecha de antemano.

Sincronización: arranca las 4 cámaras escalonadas (necesario para la
conexión USB3 Vision), espera a que las 4 estén armadas, y descarta
cualquier frame llegado antes de ese instante común -> el primer frame
retenido de cada cámara corresponde al mismo pulso físico.

Con margen tan ajusto (125fps está a solo ~227us del techo real de la
cámara, 128.64fps), usa exposición FIJA corta, nunca automática.
"""
import argparse
import os
import socket
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import cv2
from vmbpy import Camera, Frame, FrameStatus, PixelFormat, Stream, VmbSystem

CC320_PORT = 30313
STAGGER_S = 0.3
TARGET_FPS = 125.0
BAYER_TO_BGR = cv2.COLOR_BayerRG2BGR


def cc320(cmd: str, ip: str, wait: float = 0.4) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect((ip, CC320_PORT))
    s.sendall(cmd.encode() + b"\r")
    time.sleep(wait)
    r = s.recv(2048).decode(errors="replace")
    s.close()
    return r.strip()


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


def configure(cam: Camera, exposure_us: float) -> None:
    cam.set_pixel_format(PixelFormat.BayerRG8)
    # A 125fps hacen falta ~295 MB/s; el valor por defecto (200 MB/s, dejado
    # por una sesion anterior) capaba la camara a ~85fps y le hacia perder
    # casi todos los triggers al no poder seguir el ritmo del CC320.
    tlo, thi = cam.DeviceLinkThroughputLimit.get_range()
    cam.DeviceLinkThroughputLimit.set(thi)
    cam.ExposureAuto.set("Off")
    lo, hi = cam.ExposureTime.get_range()
    cam.ExposureTime.set(min(max(exposure_us, lo), hi))
    cam.TriggerSelector.set("FrameStart")
    cam.TriggerSource.set("Line0")
    cam.TriggerActivation.set("RisingEdge")
    cam.TriggerMode.set("On")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--duration", type=float, default=5.0,
                         help="duracion de la captura en segundos (default 5, "
                              "calculado para caber en RAM con margen)")
    parser.add_argument("-e", "--exposure", type=float, default=7000.0,
                         help="exposicion fija en us (default 7000 = 7ms, "
                              "margen seguro bajo el periodo de 8ms)")
    parser.add_argument("--ram-fraction", type=float, default=0.6)
    parser.add_argument("-o", "--output", type=Path,
                         default=Path(__file__).resolve().parent.parent / "captures" / "hwtrigger125")
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
        cams = [c for c in vmb.get_all_cameras() if "Simulator" not in c.get_name()]
        if not cams:
            print("No hay camaras fisicas.", file=sys.stderr)
            return 1

        results = {c.get_id(): Collector() for c in cams}
        dims = {}

        ram_budget = int(available_ram_bytes() * args.ram_fraction)
        stopped_early = False

        with ExitStack() as stack:
            opened = []
            for cam in cams:
                stack.enter_context(cam)
                configure(cam, args.exposure)
                dims[cam.get_id()] = (cam.Width.get(), cam.Height.get())
                handler = results[cam.get_id()]
                cam.start_streaming(handler=handler, buffer_count=64)
                opened.append(cam)
                print(f"{cam.get_id()}: armada (TriggerMode=On, Line0, exposicion {args.exposure:.0f}us)")
                time.sleep(STAGGER_S)

            t_ready = time.perf_counter()
            print(f"\nLas {len(opened)} camaras armadas. Abriendo el gate (RV3,1)...")
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

        if stopped_early:
            print(f"\n!! Corte de seguridad por RAM tras {elapsed:.2f}s "
                  f"(pedidos {args.duration:.1f}s)")

        # Sincronizacion: descarta lo llegado antes de que las 4 estuvieran armadas
        for res in results.values():
            res.frames = [(t, f) for t, f in res.frames if t >= t_ready]

        print(f"\n--- Resultado tras {elapsed:.2f}s a {TARGET_FPS:.0f}fps objetivo "
              f"(esperado ~{elapsed * TARGET_FPS:.0f} frames/camara) ---")
        for cid, res in results.items():
            n = len(res.frames)
            if n >= 2:
                span = res.frames[-1][0] - res.frames[0][0]
                fps_real = (n - 1) / span if span > 0 else 0
                deltas_ms = [(res.frames[i + 1][0] - res.frames[i][0]) * 1000
                             for i in range(min(5, n - 1))]
                print(f"{cid}: {n} frames  fps_real={fps_real:.2f}  "
                      f"primeros deltas(ms)={[f'{d:.2f}' for d in deltas_ms]}")
            else:
                print(f"{cid}: {n} frames")

        primeros = [res.frames[0][0] for res in results.values() if res.frames]
        if len(primeros) > 1:
            print(f"\nDesalineacion entre el primer frame de cada camara: "
                  f"{(max(primeros) - min(primeros)) * 1000:.2f} ms")

        print("\nEscribiendo .mp4 ...")
        for cid, res in results.items():
            if not res.frames:
                continue
            w, h = dims[cid]
            span = res.frames[-1][0] - res.frames[0][0]
            fps_video = max(1, round((len(res.frames) - 1) / span)) if span > 0 else 1
            out_path = args.output / f"trig125_{cid}.mp4"
            writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                      fps_video, (w, h))
            for _, raw in res.frames:
                writer.write(cv2.cvtColor(raw, BAYER_TO_BGR))
            writer.release()
            print(f"  {cid} -> {out_path}  ({len(res.frames)} frames @ {fps_video}fps)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
