import { Router, type IRouter } from "express";

const router: IRouter = Router();

router.get("/brokers/status", async (_req, res) => {
  try {
    const response = await fetch("http://localhost:8080/api/brokers/status");
    if (!response.ok) {
      res.status(response.status).json({ error: "Failed to fetch broker status from python engine" });
      return;
    }
    const data = await response.json();
    res.json(data);
  } catch (err: any) {
    res.status(503).json({ error: "Broker engine unreachable", detail: err?.message || String(err) });
  }
});

export default router;
