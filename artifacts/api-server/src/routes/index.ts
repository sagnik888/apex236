import { Router, type IRouter } from "express";
import healthRouter from "./health";
import brokersRouter from "./brokers";
import optionsRouter from "./options";

const router: IRouter = Router();

router.use(healthRouter);
router.use(brokersRouter);
router.use(optionsRouter);

export default router;
