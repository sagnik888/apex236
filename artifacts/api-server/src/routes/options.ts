import { Router, type IRouter } from "express";
import { z } from "zod";

const router: IRouter = Router();

router.get("/options/resolve", async (req, res) => {
  try {
    const params = new URLSearchParams(req.query as Record<string, string>);
    const PYTHON_ENGINE_URL = process.env.PYTHON_ENGINE_URL || "http://localhost:8080";
    const response = await fetch(`${PYTHON_ENGINE_URL}/api/options/resolve?${params.toString()}`, {
      headers: { ...(req.headers.authorization ? { Authorization: req.headers.authorization } : {}) }
    });
    const contentType = response.headers.get("content-type");
    if (contentType && contentType.includes("application/json")) {
      const data = await response.json();
      res.status(response.status).json(data);
    } else {
      const text = await response.text();
      res.status(response.status).send(text);
    }
  } catch (err: any) {
    res.status(503).json({ error: "Options engine unreachable", detail: err?.message || String(err) });
  }
});

const OptionsTradeSchema = z.object({
  underlying_symbol: z.string().min(1),
  spot_price: z.number().positive(),
  direction: z.enum(["LONG", "SHORT", "BUY", "SELL"]),
  quantity: z.number().int().positive(),
  stop_spot: z.number().positive(),
  target_spot: z.number().positive(),
  timeframe: z.string().optional().default("15m"),
  stop_mode: z.string().optional().default("Delta-Translated"),
  tag: z.string().optional(),
});

router.post("/options/trade", async (req, res) => {
  try {
    const parseResult = OptionsTradeSchema.safeParse(req.body);
    if (!parseResult.success) {
      res.status(400).json({ error: "Invalid payload", detail: parseResult.error.errors });
      return;
    }
    const PYTHON_ENGINE_URL = process.env.PYTHON_ENGINE_URL || "http://localhost:8080";
    const response = await fetch(`${PYTHON_ENGINE_URL}/api/options/trade`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(req.headers.authorization ? { Authorization: req.headers.authorization } : {}),
      },
      body: JSON.stringify(parseResult.data),
    });
    const contentType = response.headers.get("content-type");
    if (contentType && contentType.includes("application/json")) {
      const data = await response.json();
      res.status(response.status).json(data);
    } else {
      const text = await response.text();
      res.status(response.status).send(text);
    }
  } catch (err: any) {
    res.status(503).json({ error: "Options trade execution unreachable", detail: err?.message || String(err) });
  }
});

export default router;
