import { Router, type IRouter } from "express";

const router: IRouter = Router();

router.get("/options/resolve", async (req, res) => {
  try {
    const params = new URLSearchParams(req.query as Record<string, string>);
    const response = await fetch(`http://localhost:8080/api/options/resolve?${params.toString()}`);
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err: any) {
    res.status(503).json({ error: "Options engine unreachable", detail: err?.message || String(err) });
  }
});

router.post("/options/trade", async (req, res) => {
  try {
    const response = await fetch("http://localhost:8080/api/options/trade", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(req.headers.authorization ? { Authorization: req.headers.authorization } : {}),
      },
      body: JSON.stringify(req.body),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err: any) {
    res.status(503).json({ error: "Options trade execution unreachable", detail: err?.message || String(err) });
  }
});

export default router;
