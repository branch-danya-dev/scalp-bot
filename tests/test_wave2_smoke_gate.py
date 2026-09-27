import importlib.util
import ast
from pathlib import Path


def test_writer_loss_has_priority_over_no_fills(tmp_path):
    spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    result = dict(naturalFills=0, error=None, writerHealth=dict(inputWriterError="queue exceeded"))
    source = Path(spec.origin).read_text()
    assignment = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
            and t.slice.value == "acceptance" for t in node.targets))
    environment = dict(vars(smoke), result=result)
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), spec.origin, "exec"), environment)
    assert result["acceptance"] == "INVALID"
