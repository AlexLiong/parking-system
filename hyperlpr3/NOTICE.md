# HyperLPR3 inference attribution

The Python inference modules in this directory are derived from
`szad670401/HyperLPR`, PyPI `hyperlpr3==0.1.3`, Apache License 2.0.
The bundled ONNX weights come from `lets-mica/mica-ai`, `model-tools/plate/models/`,
also Apache License 2.0. The project is configured to load local weights only;
the upstream HTTP auto-downloader and unrelated CLI modules are omitted.

Upstream sources:

- https://github.com/szad670401/HyperLPR
- https://github.com/lets-mica/mica-ai

See `LICENSE` for the Apache-2.0 text. The weight hashes are checked by
`server/lpr/hyperlpr_adapter.py` before inference.
