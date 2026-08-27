#!/usr/bin/env bash
#
# Downloads the ONNX models the recognition analyzer needs.
#
# The models are not committed: together they are ~170 MB, and they are
# third-party artefacts with their own licences. This fetches them into the
# analysis model directory, verifies each one loads, and is safe to re-run.
#
# Licences, all compatible with this project being MIT:
#   yolox_tiny        Apache-2.0  (Megvii). Chosen over YOLOv8/11n specifically
#                                 because Ultralytics ships under AGPL-3.0.
#   person_reid_youtu Apache-2.0  (OpenCV Zoo)
#   yolo-v9 plate     MIT         (ankandrew/open-image-models)
#   cct_s_v2_global   MIT         (ankandrew/cnn-ocr-lp)
set -euo pipefail

MODEL_DIR="${1:-/var/lib/timelapsed/index/models}"
SERVICE_USER="${SERVICE_USER:-timelapsed}"

YOLOX_URL="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"
REID_URL="https://github.com/opencv/opencv_zoo/raw/main/models/person_reid_youtureid/person_reid_youtu_2021nov.onnx"
PLATE_DETECT_URL="https://github.com/ankandrew/open-image-models/releases/download/assets/yolo-v9-s-608-license-plates-end2end.onnx"
PLATE_OCR_URL="https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/cct_s_v2_global.onnx"

mkdir -p "${MODEL_DIR}"

fetch() {
    local name="$1" url="$2" target="${MODEL_DIR}/$1"
    if [[ -s "${target}" ]]; then
        echo "  ${name} already present ($(du -h "${target}" | cut -f1))"
        return
    fi
    echo "  fetching ${name} ..."
    # Download beside the target and move into place, so an interrupted run
    # cannot leave a truncated model that loads as a confusing error later.
    curl -fsSL --retry 3 -o "${target}.part" "${url}"
    mv "${target}.part" "${target}"
    echo "  ${name} done ($(du -h "${target}" | cut -f1))"
}

echo "Fetching recognition models into ${MODEL_DIR}"
fetch yolox_tiny.onnx "${YOLOX_URL}"
fetch reid.onnx "${REID_URL}"
fetch plate_detect.onnx "${PLATE_DETECT_URL}"
fetch plate_ocr.onnx "${PLATE_OCR_URL}"

if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${MODEL_DIR}"
fi

echo
echo "Verifying the models load..."
PYTHON="/opt/timelapsed/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
"${PYTHON}" - "${MODEL_DIR}" <<'PY'
import sys
from pathlib import Path

import onnxruntime as ort

model_dir = Path(sys.argv[1])
for name in ("yolox_tiny.onnx", "reid.onnx", "plate_detect.onnx", "plate_ocr.onnx"):
    session = ort.InferenceSession(
        str(model_dir / name), providers=["CPUExecutionProvider"]
    )
    shape = session.get_inputs()[0].shape
    print(f"  ok  {name:<20} input {shape}")
PY

echo
echo "Models ready. Enable recognition in /etc/timelapsed.ini ([analysis] enabled = true),"
echo "then: systemctl enable --now timelapsed-analyzer"
