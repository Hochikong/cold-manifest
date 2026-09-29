"""支持 python -m cold_manifest。"""

import sys

from .cli import main

sys.exit(main())
