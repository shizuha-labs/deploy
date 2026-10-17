"""Load the local signing identity before Django initializes, then exec Hive."""
import os
from pathlib import Path
import sys

os.environ['HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM'] = Path('/runtime/capability-key.pem').read_text()
os.execvp(sys.argv[1], sys.argv[1:])
