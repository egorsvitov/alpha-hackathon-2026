import sys
from pathlib import Path

# Тесты запускаются из backend/ (python -m unittest discover -s tests -t .) или pytest.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
