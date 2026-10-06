"""Domain packages discovered by the API, migrations, and Python distribution."""

# Register settings-owned persistence models for metadata consumers that import
# the domain package as the application model registry.
from modules.settings.models import OnboardingStateRecord as OnboardingStateRecord
