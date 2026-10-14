import sys
from pathlib import Path

# La radice del progetto (dove stanno main.py e template_bot.py) va nel percorso.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
