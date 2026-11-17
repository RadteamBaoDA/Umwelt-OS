"""Domain packages discovered by the API, migrations, and Python distribution.

This package intentionally imports nothing: persistence models are registered by the owning module when it is
imported (``modules.settings.onboarding`` imports its own ``OnboardingStateRecord``; Alembic's ``env.py`` imports every
model module explicitly), so no module imports another module's ORM models at package import.
"""
