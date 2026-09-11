class SpenceError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.hint = code, hint

    def to_dict(self):
        return {"code": self.code, "message": str(self), **({"hint": self.hint} if self.hint else {})}
