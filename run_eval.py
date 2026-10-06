import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv

load_dotenv()
os.environ["BOT_URL"] = "http://127.0.0.1:8000"
os.environ["LLM_MODEL"] = "gemini-3.5-flash-lite"

import judge_simulator
judge_simulator.BOT_URL = "http://127.0.0.1:8000"
judge_simulator.LLM_MODEL = "gemini-3.5-flash-lite"

provider = judge_simulator.create_provider()
judge = judge_simulator.JudgeSimulator(provider)

scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
print(f"Running scenario: {scenario}")
success = judge.run(scenario)
print(f"Result: {success}")
