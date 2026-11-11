from pathlib import Path

_MODEL_VERSION_ = "20230229"

_DEFAULT_FOLDER_ = str(Path(__file__).resolve().parents[2] / "server/lpr/models/hyperlpr3")

onnx_runtime_config = dict(
    det_model_path_320x=str(Path(_MODEL_VERSION_) / "onnx/y5fu_320x_sim.onnx"),
    det_model_path_640x=str(Path(_MODEL_VERSION_) / "onnx/y5fu_640x_sim.onnx"),
    rec_model_path=str(Path(_MODEL_VERSION_) / "onnx/rpv3_mdict_160_r3.onnx"),
    cls_model_path=str(Path(_MODEL_VERSION_) / "onnx/litemodel_cls_96x_r1.onnx"),
)
