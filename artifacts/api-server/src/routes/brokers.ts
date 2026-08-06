import { Router, type IRouter } from "express";

const router: IRouter = Router();

router.get("/brokers/status", async (req, res) => {
  try {
    const PYTHON_ENGINE_URL = process.env.PYTHON_ENGINE_URL || "http://localhost:8080";
    const response = await fetch(`${PYTHON_ENGINE_URL}/api/brokers/status`, {
      headers: { ...(req.headers.authorization ? { Authorization: req.headers.authorization } : {}) }
    });
    if (!response.ok) {
      res.status(response.status).json({ error: "Failed to fetch broker status from python engine" });
      return;
    }
    const contentType = response.headers.get("content-type");
    if (contentType && contentType.includes("application/json")) {
      const data = await response.json();
      res.json(data);
    } else {
      const text = await response.text();
      res.send(text);
    }
  } catch (err: any) {
    res.status(503).json({ error: "Broker engine unreachable", detail: err?.message || String(err) });
  }
});

export default router;
