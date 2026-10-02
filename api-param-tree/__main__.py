from .server import serve
import os
base = os.path.dirname(os.path.abspath(__file__))
serve(os.path.join(base, 'data', 'app.db'), os.path.join(base, 'static'))
