# PreMov

Utilidades en Python para capturar vídeo simultáneo de 4 cámaras industriales
Allied Vision Alvium 1800 U-240c (USB3 Vision), conectadas a través de una
placa PCIe con hub USB3 de 4 puertos. Incluye captura en software a máxima
velocidad y captura sincronizada por hardware mediante un controlador de
disparo externo (Gardasoft CC320).

## Hardware necesario

- 4 cámaras Allied Vision Alvium 1800 U-240c (USB3 Vision), cada una
  conectada a un puerto USB3 independiente del hub.
- Una placa PCIe con hub USB3 de 4 puertos (probado con IOI
  U3X4-PCIE4XE304, basada en un switch Pericom + 4 controladores Renesas
  uPD720202, uno por puerto — cada cámara tiene su propio canal USB3
  completo, no hay reparto de ancho de banda entre ellas).
- (Opcional, solo para captura sincronizada por hardware) un controlador
  Gardasoft CC320, con su salida de disparo cableada a la línea `Line0` de
  cada cámara, y conectado a la misma red local que el equipo por Ethernet.

## Software necesario

- Ubuntu 26.04 LTS (o similar).
- [VimbaX SDK](https://www.alliedvision.com/en/products/software/vimba-x-sdk/)
  de Allied Vision — **no se distribuye en este repositorio** ni por pip:
  hay que descargarlo del sitio de Allied Vision (requiere cuenta gratuita)
  e instalarlo en `/opt`.
- Conda (miniforge/miniconda/anaconda) para el entorno de Python.

## Instalación en una máquina nueva

1. **Descargar este repositorio.** Si no tienes el código todavía:
   ```bash
   git clone https://github.com/carmelocuenca/PreMov.git
   cd PreMov
   ```

2. **Instalar VimbaX.** Descarga el instalador de Allied Vision para Linux
   x86_64 y ejecútalo como root. Se instala en `/opt/VimbaX_<versión>/` e
   incluye un script que configura la variable de entorno
   `GENICAM_GENTL64_PATH` de forma permanente (crea
   `/etc/profile.d/VimbaX_GenTL_Path_64bit.sh`). Reinicia la sesión tras
   instalarlo.

3. **Añadir el usuario al grupo `video`.** La regla udev de VimbaX
   (`/etc/udev/rules.d/99-AVTUSBTL.rules`) intenta dar acceso a estas
   cámaras a cualquier usuario (`MODE="0666"`), pero si también hay
   instalado el SDK CVB de Stemmer Imaging (paquete `cvb*` en `/opt`),
   su regla `59-cvb_u3v.rules` reconoce antes estos mismos dispositivos
   por su clase USB3 Vision genérica y les asigna `GROUP="video"` con
   permisos más restrictivos — esa es la que gana en la práctica. Sin
   estar en ese grupo, `vmbpy`/VimbaX no verá ninguna cámara aunque
   estén bien conectadas. Comprueba los nodos reales con
   `ls -l /dev/bus/usb/*/*` si quieres verificarlo.
   ```
   sudo usermod -aG video $USER
   ```
   Cierra la sesión y vuelve a entrar (la pertenencia a un grupo nuevo
   no se aplica a una sesión ya abierta).

4. **Comprobar que las cámaras se detectan.** Con las 4 cámaras conectadas
   al hub USB, ejecuta:
   ```
   /opt/VimbaX_<versión>/bin/ListCameras_VmbCPP
   ```
   o abre `VimbaXViewer` (misma carpeta `bin/`). Debes ver las 4 cámaras
   físicas (si solo ves "no transport layers were found", revisa el paso 2;
   si las ves pero fallan al abrir por permisos, revisa el paso 3).

5. **Instalar conda, si el usuario que vas a usar no lo tiene ya.** Cada
   usuario del sistema necesita su propia instalación (conda vive en el
   `$HOME` de quien lo instala, no es compartido entre usuarios). Con
   Miniforge, de forma no interactiva:
   ```
   cd ~
   curl -L -O https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
   bash Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"
   source "$HOME/miniforge3/etc/profile.d/conda.sh"
   ```
   La última línea (`source .../conda.sh`) hay que repetirla en cada
   sesión nueva de terminal hasta que corras `conda init` o abras una
   sesión de login completa (el instalador ya añade el bloque necesario a
   `~/.bashrc` para sesiones interactivas futuras).

6. **Crear el entorno conda:**
   ```
   conda env create -f environment.yml
   conda activate PreMov
   ```

7. **Instalar vmbpy** (las bindings de Python de VimbaX). Vienen dentro del
   propio SDK, no en PyPI — instala el wheel correspondiente a la versión
   instalada en el paso 2, con el entorno `PreMov` activo:
   ```
   pip install /opt/VimbaX_<versión>/api/python/vmbpy-*.whl
   ```

8. **(Solo si usas el CC320)** copia `.env.example` a `.env` y ajusta la IP
   a la de tu controlador:
   ```
   cp .env.example .env
   # edita .env con la IP real
   export $(grep -v '^#' .env | xargs)
   ```

9. **Probar.** Con el entorno activado y `GENICAM_GENTL64_PATH` presente en
   la sesión (`echo $GENICAM_GENTL64_PATH`):
   ```
   python scripts/capture_frames.py
   ```
   Debería generar un `.png` por cada cámara física en `captures/`.

## Cómo funciona

### Captura de un fotograma (`scripts/capture_frames.py`)

Prueba rápida de conectividad: abre cada cámara física, pide un único
fotograma y lo guarda como PNG. Útil para comprobar que una cámara
concreta responde antes de lanzar pruebas más largas.

```
python scripts/capture_frames.py              # solo cámaras físicas
python scripts/capture_frames.py --all         # incluye las simuladas de VimbaX
python scripts/capture_frames.py -o /ruta/salida
```

### Captura de velocidad en software (`scripts/speed_test_capture.py`)

Arranca el streaming de las 4 cámaras a la vez (con un pequeño retardo
escalonado entre cada una, necesario para que el hub USB3 no rechace la
negociación de conexión si se abren exactamente en el mismo instante) y
graba cada una a su `.mp4` en `captures/speedtest/`. Mide el throughput real
alcanzado (fps y MB/s) por cámara y en total.

Ejemplos:
```
# 15s a máxima velocidad, exposición fija
python scripts/speed_test_capture.py -d 15 -e 4000

# auto-exposición (necesario si las cámaras no reciben la misma luz)
python scripts/speed_test_capture.py -d 15 --auto-exposure

# fps objetivo concreto, garantizando que se cumple aunque haya poca luz
python scripts/speed_test_capture.py --target-fps 60 --auto-exposure --guarantee-fps

# fps objetivo concreto con duración corta (p.ej. 2.5s a 125fps): con la
# exposición manual por defecto (4000us, más corta que el periodo de 8ms
# que exige 125fps) no hace falta tocar nada más
python scripts/speed_test_capture.py -d 2.5 --target-fps 125

# a mitad de resolución por binning (más fps)
python scripts/speed_test_capture.py -b 2

# resolución 1K estándar, recortada centrada en el sensor (más fps que a
# resolución completa: 143.67fps medidos frente a 128.64fps)
python scripts/speed_test_capture.py --resolution 1920x1080

# recorte que alcanza los 176fps reales de esta cámara (su techo a
# resolución completa son 128.64fps)
python scripts/speed_test_capture.py --resolution 1936x862
```

Por defecto (sin `--resolution`) usa la resolución máxima del sensor. Un
valor menor recorta la imagen centrada (no reescala) — por eso recortes
más pequeños permiten más fps, al leer menos píxeles del sensor por
fotograma.

El techo real medido de esta cámara es 128.64fps, así que un `--target-fps`
cercano a ese valor (como 125) está muy próximo al límite pero debería
cumplirse sin problema — esto es captura en software libre, sin relación
con la no determinación que sí aparece al disparar las 4 cámaras a la vez
por hardware a esa misma frecuencia (ver más abajo, CC320).

Incluye un guardia de seguridad de RAM: los fotogramas se acumulan sin
comprimir en memoria mientras dura la captura (pueden ser varios GB/s con
las 4 cámaras), así que si el uso llega al 60% de la RAM disponible
(configurable con `--ram-fraction`) la captura se corta antes de forzar swap.

### Captura sincronizada por hardware (CC320)

Con el controlador Gardasoft CC320 generando los pulsos de disparo hacia
`Line0` de cada cámara (la configuración del propio CC320 — periodo,
anchura de pulso — se hace desde su interfaz web/teclado, no desde estos
scripts):

- `scripts/hw_trigger_test.py` — prueba con **una sola cámara**: abre la
  puerta del CC320 (arrancan los pulsos), captura N segundos y guarda cada
  fotograma como PNG con su instante de llegada. Pide la IP del CC320 por
  `--cc320-ip` o por la variable de entorno `PREMOV_CC320_IP`.
  ```
  python scripts/hw_trigger_test.py -c DEV_1AB22C0C4403 -d 30
  ```

- `scripts/hw_trigger_4cam.py` — las **4 cámaras a la vez**, en modo
  pasivo: no manda ningún comando al CC320 (asume que ya está disparando),
  solo escucha y comprueba que las 4 reciben el mismo pulso sincronizadas
  entre sí (reporta la desalineación en ms entre el primer fotograma
  retenido de cada cámara).
  ```
  python scripts/hw_trigger_4cam.py -d 30
  ```

- `scripts/hw_trigger_capture.py` — las **4 cámaras a la vez**, con fps y
  resolución configurables. Siempre **lee primero** (comando `ST`) el
  periodo de trigger que el CC320 ya tiene programado:

  - sin `--fps`, lo deja tal cual, y lo usa para calcular una exposición
    segura por defecto (90% del periodo);
  - con `--fps`, si ya coincide tampoco toca nada; si no coincide, lo
    **reprograma** (comando `RB1,p` — manual CC320 sección 10.3 — un único
    envío, sin guardarlo de forma permanente con `AW`) y vuelve a leer para
    confirmar el cambio antes de capturar.

  También abre/cierra la puerta del CC320 (`RV3,1` / `RV3,0`, un único
  comando cada vez). En total, como mucho son 5 comandos por ejecución
  (leer, programar, releer, abrir puerta, cerrar puerta) — nunca una
  ráfaga de reconfiguraciones seguidas, que es lo que bloqueó el CC320 una
  vez en este proyecto. La duración (`-d`) es obligatoria; fps y
  resolución son opcionales:

  ```
  # usa el fps que ya esté programado en el CC320, resolución máxima
  python scripts/hw_trigger_capture.py -d 5

  # resolución recortada (más fps posibles)
  python scripts/hw_trigger_capture.py -d 5 --resolution 1920x1080

  # si el CC320 no está ya a 60fps, lo reprograma antes de capturar
  python scripts/hw_trigger_capture.py -d 5 --fps 60
  ```

  El periodo del CC320 se fija en pasos de 0.1ms, así que el fps real tras
  programarlo puede no ser exacto (p.ej. pedir 60fps deja el CC320 a
  59.88fps, el más cercano posible). El mensaje de confirmación muestra el
  valor real aplicado.

  Si hay un error (de red con el CC320, de cámara, lo que sea), el script
  no intenta recuperarse ni reintentar: para limpiamente y muestra el tipo
  de excepción y su mensaje.

### Comprobar visualmente la sincronización (`scripts/make_mosaic.py`)

Toma los 4 vídeos de una captura sincronizada y monta un mosaico 2x2 (cada
uno reducido al 50%), para ver a simple vista si las 4 cámaras van a la
par. Útil sobre todo si las imágenes llevan un reloj en pantalla (p.ej.
apuntando las 4 cámaras a un cronómetro de milisegundos): en el mosaico se
ve de un vistazo si las 4 marcan lo mismo o si alguna va desfasada.

```
python scripts/make_mosaic.py \
  captures/hwtrigger/hwtrig_<ID1>.mp4 \
  captures/hwtrigger/hwtrig_<ID2>.mp4 \
  captures/hwtrigger/hwtrig_<ID3>.mp4 \
  captures/hwtrigger/hwtrig_<ID4>.mp4
```

Genera `mosaic_2x2.mp4` junto a los vídeos de entrada (o en `-o DIR` si se
indica), más una copia reducida al 50% de cada vídeo individual
(`half_<nombre>.mp4`). No depende de qué script haya generado los 4
vídeos de entrada — sirve igual para los de `hw_trigger_capture.py` que
para los de `speed_test_capture.py`.

### Diagnóstico del bus USB (`scripts/probe_dropout_timing.py`,
`scripts/probe_staggered_start.py`)

Scripts de diagnóstico que no graban vídeo, usados para caracterizar por
qué fallaba la conexión simultánea de las 4 cámaras (ver el código: ambos
tienen una docstring explicando qué comprueba cada uno). Útiles si se
cambia de hub USB o de cámaras y vuelven a aparecer caídas de conexión.
```
python scripts/probe_dropout_timing.py
python scripts/probe_staggered_start.py -s 300
```

## Salida

Todos los scripts escriben en `captures/<subcarpeta>/`, que no se versiona
en git (son ficheros binarios pesados, no código — ver `.gitignore`). Cada
ejecución crea la carpeta de salida si no existe.
