"""ONNX model wrappers.

Every model is driven directly through onnxruntime rather than through the
library that publishes it. Those libraries pull in OpenCV and a torch-adjacent
stack; this guest has 6 GB and already runs six capture workers plus ffmpeg, so
the whole subsystem is held to onnxruntime + numpy + Pillow.

Nothing here decides policy. Thresholds live in the config, and the pipeline
decides what a detection means.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

logger = logging.getLogger(__name__)

# COCO ids, collapsed to the two kinds the timeline shows. Bicycles count as
# vehicles: at these distances the detector confuses them with motorcycles, and
# for "did something roll past the gate" the distinction does not matter.
COCO_KINDS = {0: "person", 1: "vehicle", 2: "vehicle", 3: "vehicle", 5: "vehicle", 7: "vehicle"}

# fast-plate-ocr's Latin alphabet. Index 36 ('_') is the pad slot.
PLATE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
PLATE_PAD = "_"
# Position of 'Brazil' in the model's region head. A plate the model thinks is
# from somewhere else is nearly always a misread of something that is not a plate.
BRAZIL_REGION_INDEX = 11

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class Detection:
    kind: str
    score: float
    box: tuple[int, int, int, int]  # x, y, w, h in source pixels


def _session(model_path: Path, threads: int) -> ort.InferenceSession:
    if not model_path.exists():
        raise FileNotFoundError(
            f"Model not found at {model_path}. Run deploy/fetch-models.sh."
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        str(model_path), options, providers=["CPUExecutionProvider"]
    )


def _letterbox(image: np.ndarray, size: int, pad_value: int) -> tuple[np.ndarray, float, float, float]:
    """Resize preserving aspect ratio, pad to square. Returns the inverse transform."""
    ratio = min(size / image.shape[0], size / image.shape[1])
    height, width = int(round(image.shape[0] * ratio)), int(round(image.shape[1] * ratio))
    resized = np.asarray(Image.fromarray(image).resize((width, height), Image.BILINEAR))
    canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
    canvas[:height, :width] = resized
    return canvas, ratio, 0.0, 0.0


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        best = order[0]
        keep.append(int(best))
        xx1 = np.maximum(x1[best], x1[order[1:]])
        yy1 = np.maximum(y1[best], y1[order[1:]])
        xx2 = np.minimum(x2[best], x2[order[1:]])
        yy2 = np.minimum(y2[best], y2[order[1:]])
        overlap = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = overlap / (areas[best] + areas[order[1:]] - overlap)
        order = order[np.where(iou <= threshold)[0] + 1]
    return keep


class ObjectDetector:
    """YOLOX-tiny over the whole frame, finding people and vehicles.

    Apache-2.0, unlike the Ultralytics YOLO family, which is AGPL-3.0 and would
    sit awkwardly against this project's MIT licence.
    """

    def __init__(self, model_path: Path, threads: int = 2, nms_threshold: float = 0.45):
        self.session = _session(model_path, threads)
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.size = int(shape[2])
        self.nms_threshold = nms_threshold

    def __call__(self, image: np.ndarray, score_threshold: float) -> list[Detection]:
        """`image` is RGB uint8. YOLOX wants BGR at 0-255 with no normalisation."""
        canvas, ratio, _, _ = _letterbox(image, self.size, 114)
        tensor = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32)
        raw = self.session.run(None, {self.input_name: np.ascontiguousarray(tensor)})[0]

        predictions = self._decode(raw)[0]
        scores = predictions[:, 4:5] * predictions[:, 5:]
        classes = scores.argmax(1)
        confidence = scores.max(1)

        wanted = np.isin(classes, list(COCO_KINDS)) & (confidence >= score_threshold)
        if not wanted.any():
            return []

        centres, sizes = predictions[wanted, :2], predictions[wanted, 2:4]
        boxes = np.empty((int(wanted.sum()), 4), dtype=np.float32)
        boxes[:, :2] = centres - sizes / 2
        boxes[:, 2:] = centres + sizes / 2
        boxes /= ratio
        kept_classes, kept_scores = classes[wanted], confidence[wanted]

        height, width = image.shape[:2]
        found = []
        for index in _nms(boxes, kept_scores, self.nms_threshold):
            x1 = max(0, int(boxes[index][0]))
            y1 = max(0, int(boxes[index][1]))
            x2 = min(width, int(boxes[index][2]))
            y2 = min(height, int(boxes[index][3]))
            if x2 <= x1 or y2 <= y1:
                continue
            found.append(Detection(
                kind=COCO_KINDS[int(kept_classes[index])],
                score=float(kept_scores[index]),
                box=(x1, y1, x2 - x1, y2 - y1),
            ))
        return found

    def _decode(self, output: np.ndarray) -> np.ndarray:
        """Undo YOLOX's per-stride grid encoding into absolute xywh."""
        grids, strides = [], []
        for stride in (8, 16, 32):
            cells = self.size // stride
            xv, yv = np.meshgrid(np.arange(cells), np.arange(cells))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            strides.append(np.full((1, grid.shape[1], 1), stride))
        grids = np.concatenate(grids, 1)
        strides = np.concatenate(strides, 1)
        output[..., :2] = (output[..., :2] + grids) * strides
        output[..., 2:4] = np.exp(output[..., 2:4]) * strides
        return output


class BodyEmbedder:
    """Person re-identification from body appearance.

    This exists because face recognition does not work here: faces on this
    footage top out at 38px against the ~80px SFace needs. Bodies are ~347px
    tall, which is what re-ID models are trained on.

    What it gives is "the same person, in the same clothes" -- good within a day,
    meaningless across a change of outfit. Callers must not present it as identity.
    """

    def __init__(self, model_path: Path, threads: int = 2):
        self.session = _session(model_path, threads)
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.height, self.width = int(shape[2]), int(shape[3])

    def __call__(self, crop_rgb: np.ndarray) -> np.ndarray:
        resized = np.asarray(
            Image.fromarray(crop_rgb).resize((self.width, self.height), Image.BICUBIC)
        )
        tensor = (resized.astype(np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        tensor = tensor.transpose(2, 0, 1)[None]
        vector = self.session.run(
            None, {self.input_name: np.ascontiguousarray(tensor)}
        )[0].flatten()
        return vector / (np.linalg.norm(vector) + 1e-9)


@dataclass(frozen=True)
class PlateRead:
    text: str
    confidence: float
    box: tuple[int, int, int, int]  # relative to the crop it was found in
    is_brazilian_region: bool


class PlateReader:
    """Detect a plate inside a vehicle crop, then read it.

    Both halves are needed because reading is only meaningful on a tight plate
    box. Reads at this resolution are unreliable per frame -- see
    docs/Recognition-Feasibility.md -- so the pipeline votes across an event
    rather than trusting any single result from here.
    """

    def __init__(self, detector_path: Path, ocr_path: Path, threads: int = 2):
        self.detector = _session(detector_path, threads)
        self.detector_input = self.detector.get_inputs()[0].name
        self.detector_size = int(self.detector.get_inputs()[0].shape[2])

        self.ocr = _session(ocr_path, threads)
        self.ocr_input = self.ocr.get_inputs()[0].name
        ocr_shape = self.ocr.get_inputs()[0].shape
        self.ocr_height, self.ocr_width = int(ocr_shape[1]), int(ocr_shape[2])

    def __call__(self, crop_rgb: np.ndarray, detect_threshold: float = 0.4) -> list[PlateRead]:
        canvas, ratio, _, _ = _letterbox(crop_rgb, self.detector_size, 114)
        tensor = (canvas.transpose(2, 0, 1)[None] / 255.0).astype(np.float32)
        raw = self.detector.run(
            None, {self.detector_input: np.ascontiguousarray(tensor)}
        )[0]
        if raw.size == 0:
            return []

        reads = []
        height, width = crop_rgb.shape[:2]
        # Columns are [batch, x1, y1, x2, y2, class, score].
        for row in raw:
            score = float(row[6])
            if score < detect_threshold:
                continue
            x1 = max(0, int(row[1] / ratio))
            y1 = max(0, int(row[2] / ratio))
            x2 = min(width, int(row[3] / ratio))
            y2 = min(height, int(row[4] / ratio))
            if x2 - x1 < 8 or y2 - y1 < 4:
                continue

            text, confidence, region = self._read(crop_rgb[y1:y2, x1:x2])
            reads.append(PlateRead(
                text=text,
                confidence=confidence,
                box=(x1, y1, x2 - x1, y2 - y1),
                is_brazilian_region=region == BRAZIL_REGION_INDEX,
            ))
        return reads

    def _read(self, plate_rgb: np.ndarray) -> tuple[str, float, int]:
        resized = np.asarray(
            Image.fromarray(plate_rgb).resize((self.ocr_width, self.ocr_height), Image.BILINEAR)
        )
        # This model takes uint8 straight through; it normalises internally.
        tensor = resized.astype(np.uint8)[None]
        plate_logits, region_logits = self.ocr.run(None, {self.ocr_input: tensor})

        # Already softmaxed inside the graph -- each slot's 37 values sum to 1.
        # Applying softmax again here reads as ~0.05 confidence on a perfect
        # read, which silently fails every confidence guard downstream.
        probabilities = plate_logits[0]
        indices = probabilities.argmax(axis=1)

        characters, confidences = [], []
        for slot, index in enumerate(indices):
            character = PLATE_ALPHABET[index]
            if character == PLATE_PAD:
                continue
            characters.append(character)
            confidences.append(float(probabilities[slot, index]))

        text = "".join(characters)
        confidence = float(np.mean(confidences)) if confidences else 0.0
        return text, confidence, int(region_logits[0].argmax())
