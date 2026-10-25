"""Compatibility facade: the pipeline lives in the `studio` package.

Existing callers (`app.py`, `screenwriter.py`, `desktop_app.py`, tests) keep
importing from `generator`; new code should import from `studio.<module>`.
"""

from studio.common import *  # noqa: F401,F403
from studio.common import _load_default_gemini_key  # noqa: F401
from studio.emotion import *  # noqa: F401,F403
from studio.render import *  # noqa: F401,F403
from studio.script import *  # noqa: F401,F403
from studio.script import _is_male_voice_id  # noqa: F401
from studio.thumbnail import *  # noqa: F401,F403
from studio.tts import *  # noqa: F401,F403
from studio.video import *  # noqa: F401,F403
