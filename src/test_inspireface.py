"""Smoke test for InspireFace engine bootstrap."""

from src.core.inspireface_engine import create_session, model_dir


def main() -> None:
    print(f"Model path: {model_dir()}")
    session = create_session()
    print("InspireFace session OK:", session is not None)


if __name__ == "__main__":
    main()
