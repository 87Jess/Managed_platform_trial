class EchoAgent:
    def query(self, message: str) -> dict:
        return {"reply": message}


root_agent = EchoAgent()