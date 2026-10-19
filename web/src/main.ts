import "./styles.css";
import { App } from "./ui/app";
import { ApiView } from "./ui/views/api";
import { DiagView } from "./ui/views/diag";
import { HistoryView } from "./ui/views/history";
import { MonitorView } from "./ui/views/monitor";
import { TestView } from "./ui/views/test";

const app = new App();
// Only the pages this site shows get routes; the server lists them on <html data-pages>.
if (app.pages.has("test")) {
  app.route(/^\/$/, () => new TestView(app)).route(/^\/r\/(?<rid>[\w-]+)$/, () => new TestView(app));
}
if (app.pages.has("monitor")) app.route(/^\/monitor$/, () => new MonitorView(app));
if (app.pages.has("diag")) app.route(/^\/diag$/, () => new DiagView(app));
if (app.pages.has("history")) app.route(/^\/history$/, () => new HistoryView(app));
if (app.pages.has("api")) app.route(/^\/api$/, () => new ApiView(app));

void app.start();
