import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./index.css";
import App from "./App";
import DemoBanner from "./DemoBanner";
import DemoTour from "./DemoTour";

// Bajo automatización (pruebas E2E, varias páginas a la vez) el fondo no se anima: el desenfoque de cada superficie se recalcula en cada cuadro y satura la CPU de un runner sin GPU.
if (navigator.webdriver) document.documentElement.dataset.static = "1";

createRoot(document.getElementById("root")!).render(
  <StrictMode><div className="h-full flex flex-col"><DemoBanner /><DemoTour /><div className="flex-1 min-h-0"><App /></div></div></StrictMode>);
