
"""Ingestion module package.

Loading the public facade first keeps the import order independent of the entry point: the private ``_*`` modules import
other modules' public contracts, which import this facade back, so importing a private module directly (a test or tool)
must not start the cycle from the middle.
"""

import modules.ingestion.public  # noqa: F401
