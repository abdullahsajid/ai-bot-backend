module.exports = {
  apps: [
    {
      name: "pulse-api",
      script: "venv/bin/python3",
      args: "-m backend.main",
      cwd: "./",
      watch: false,
      max_memory_restart: "500M",
      env: {
        PORT: 8000,
        PYTHONPATH: "."
      }
    },
    {
      name: "pulse-discord",
      script: "venv/bin/python3",
      args: "-m backend.bots.discord_bot",
      cwd: "./",
      watch: false,
      env: {
        PYTHONPATH: "."
      }
    },
    {
      name: "pulse-telegram",
      script: "venv/bin/python3",
      args: "-m backend.bots.telegram_bot",
      cwd: "./",
      watch: false,
      env: {
        PYTHONPATH: "."
      }
    }
  ]
};
