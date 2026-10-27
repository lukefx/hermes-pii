"""Run an offline detector self-check before enabling the plugin."""
import argparse
import json

from .plugin import PiiPlugin, _require_abort_support


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name", default="en_core_web_sm")
    parser.add_argument("--language", default="en")
    parser.add_argument("--score-threshold", type=float, default=0.4)
    parser.add_argument("--require-abort", action="store_true", help="Also require strict-mode host support")
    args = parser.parse_args(argv)
    status = PiiPlugin(args.language, args.model_name, args.score_threshold).check_health()
    if args.require_abort:
        try:
            _require_abort_support()
        except Exception as error:
            status.update(ready=False, error_type=type(error).__name__, abort_supported=False)
        else:
            status["abort_supported"] = True
    print(json.dumps(status))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
