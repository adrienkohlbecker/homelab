from pyinfra.operations import apt, server

apt.update(
  name="Update apt repositories",
  cache_time=3600,
)

apt.packages(
  name="Ensure the vim apt package is installed",
  packages=["vim"],
)

if __name__ == "__main__":
    print("uv run pyinfra inventory.py deploy.py")
