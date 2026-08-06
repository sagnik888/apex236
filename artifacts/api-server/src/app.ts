import express, { type Express } from "express";
import cors from "cors";
import { createProxyMiddleware, fixRequestBody } from "http-proxy-middleware";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
const allowedOrigins = process.env.ALLOWED_ORIGINS 
  ? process.env.ALLOWED_ORIGINS.split(",") 
  : ["http://localhost:5173", "http://localhost:3000"];

app.use(
  cors({
    origin: allowedOrigins,
    credentials: true,
  })
);
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

// Proxy fallback for unmatched /api routes to the Python engine
const PYTHON_ENGINE_URL = process.env.PYTHON_ENGINE_URL || "http://localhost:8080";
app.use(
  "/api",
  createProxyMiddleware({
    target: PYTHON_ENGINE_URL,
    changeOrigin: true,
    ws: true,
    pathRewrite: {
      "^/api": "/", // Optional depending on how python server expects it
    },
    on: { proxyReq: fixRequestBody }
  })
);

export default app;
