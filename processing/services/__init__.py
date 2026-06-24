from .supabase_client import SupabaseService

__all__ = ["SupabaseService", "BlobStorageService"]


def __getattr__(name: str):
    if name == "BlobStorageService":
        from .blob_storage import BlobStorageService
        return BlobStorageService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
