import "./styles.css";
import { App } from "./ui/app";
import { ApiView } from "./ui/views/api";
import { HistoryView } from "./ui/views/history";
import { MonitorView } from "./ui/views/monitor";
import { TestView } from "./ui/views/test";

const app = new App();
app
  .route(/^\/$/, () => new TestView(app))
  .route(/^\/r\/(?<rid>[\w-]+)$/, () => new TestView(app))
  .route(/^\/monitor$/, () => new MonitorView(app))
  .route(/^\/history$/, () => new HistoryView(app))
  .route(/^\/api$/, () => new ApiView(app));

void app.start();
