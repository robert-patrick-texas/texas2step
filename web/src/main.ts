import "./styles.css";
import { App } from "./ui/app";
import { ApiView } from "./ui/views/api";
import { DiagView } from "./ui/views/diag";
import { HelpView } from "./ui/views/help";
import { HistoryView } from "./ui/views/history";
import { MonitorView } from "./ui/views/monitor";
import { TestView } from "./ui/views/test";

const app = new App();
// A route goes only where its page is one this visitor gets (App.pages): the ones the
// server lists on <html data-pages>, and those a support pass opens. A saved result's
// address only where this visitor opens saved results.
app
  .route("test", /^\/$/, () => new TestView(app))
  .route("test", /^\/r\/(?<rid>[\w-]+)$/, () => new TestView(app), () => app.results)
  .route("monitor", /^\/monitor$/, () => new MonitorView(app))
  .route("diag", /^\/diag$/, () => new DiagView(app))
  .route("history", /^\/history$/, () => new HistoryView(app))
  .route("help", /^\/help$/, () => new HelpView(app))
  .route("api", /^\/api$/, () => new ApiView(app));

void app.start();
