"""Cloud FORWARD inference: the SAME int8 LSTM the firmware embeds
(lstm_hs30_int8_pt.tflite). Heavy deps (numpy + a tflite interpreter) are imported
lazily so the rest of the backend runs without them; is_available() reports whether
this adapter can actually infer.

Pipeline mirrors savia_c/src/system/lstm_input.c:
  real inputs -> StandardScaler -> int8 quantize (per tensor) -> model ->
  int8 dequantize -> StandardScaler inverse (HS30) -> VWC 0..1
"""
from __future__ import annotations

from ...domain.ports import InferencePort

# StandardScaler params, copied from savia_c/src/system/scaler.c (which bakes them
# from docs/.../scaler_params.json, sklearn 1.6.1). Order: [HS30, TA, HS10].
_MEAN = (0.7712527688624472, 24.38746466771412, 0.7902161245631403)
_STD = (0.042382551037717174, 5.069760003904925, 0.04550010247318015)
_HS30, _TA, _HS10 = 0, 1, 2


def _scale(x: float, feat: int) -> float:
    return (x - _MEAN[feat]) / _STD[feat]


def _inverse_hs30(x: float) -> float:
    return x * _STD[_HS30] + _MEAN[_HS30]


def _interpreter_cls():
    """Return an Interpreter class from LiteRT, tflite-runtime, or TensorFlow."""
    try:
        from ai_edge_litert.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    try:
        from tflite_runtime.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    try:
        import tensorflow as tf
        return tf.lite.Interpreter
    except ImportError:
        pass
    raise RuntimeError(
        "no tflite interpreter available "
        "(install ai-edge-litert, tflite-runtime, or tensorflow)"
    )


def _load_interpreter(model_path: str):
    it = _interpreter_cls()(model_path=model_path)
    it.allocate_tensors()
    return it


class LstmInference(InferencePort):
    def __init__(self, model_path: str):
        self._model_path = model_path
        self._it = None
        self._past_in = None
        self._future_in = None
        self._out = None

    def _ensure_loaded(self) -> None:
        if self._it is not None:
            return
        self._it = _load_interpreter(self._model_path)
        for d in self._it.get_input_details():
            shape = tuple(int(x) for x in d["shape"][1:])
            if shape == (48, 3):
                self._past_in = d
            elif shape == (24, 1):
                self._future_in = d
        self._out = self._it.get_output_details()[0]
        if self._past_in is None or self._future_in is None:
            raise RuntimeError("model does not expose the expected (48,3)+(24,1) inputs")

    def is_available(self) -> bool:
        try:
            self._ensure_loaded()
            return True
        except Exception:
            return False

    def predict_hs30(self, ta_past, hs10_past, hs30_past, ta_future) -> list[float]:
        import numpy as np

        self._ensure_loaded()
        n = len(ta_past)
        past = np.empty((1, n, 3), dtype=np.float32)
        for t in range(n):
            past[0, t, 0] = _scale(ta_past[t], _TA)      # model order [TA, HS10, HS30]
            past[0, t, 1] = _scale(hs10_past[t], _HS10)
            past[0, t, 2] = _scale(hs30_past[t], _HS30)
        future = np.array(
            [[_scale(v, _TA)] for v in ta_future], dtype=np.float32
        ).reshape(1, len(ta_future), 1)

        self._it.set_tensor(self._past_in["index"], _quantize(np, past, self._past_in))
        self._it.set_tensor(self._future_in["index"], _quantize(np, future, self._future_in))
        self._it.invoke()
        scaled = _dequantize(np, self._it.get_tensor(self._out["index"]), self._out)[0]
        return [_inverse_hs30(float(v)) for v in scaled]


def _quantize(np, arr, detail):
    """Quantize a scaled float array into the tensor's dtype (int8 or float32)."""
    scale, zero = detail["quantization"]
    if detail["dtype"] == np.float32 or scale == 0:
        return arr.astype(np.float32)
    q = np.round(arr / scale) + zero
    return np.clip(q, -128, 127).astype(detail["dtype"])


def _dequantize(np, arr, detail):
    scale, zero = detail["quantization"]
    if detail["dtype"] == np.float32 or scale == 0:
        return arr.astype(np.float32)
    return (arr.astype(np.float32) - zero) * scale
