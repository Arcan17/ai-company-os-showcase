import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./index.css";
import App from "./App";
import DemoBanner from "./DemoBanner";

createRoot(document.getElementById("root")!).render(
  <StrictMode><div className="h-full flex flex-col"><DemoBanner /><div className="flex-1 min-h-0"><App /></div></div></StrictMode>);
