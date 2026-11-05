"""Internal implementation modules for the Connectors public contract.

Import from ``modules.connectors.public``; these modules are private to the Connectors module.
"""

# Entry-point normalisation: the facade imports its ``service.*`` implementation in a dependency-safe order, while
# Ingestion/Observations contracts that these modules import import the facade back. Loading the facade first (even
# when a test or tool imports a ``service.*`` module directly) keeps that order independent of the entry point.
import modules.connectors.public  # noqa: F401
