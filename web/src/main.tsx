import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Demo from "./Demo";
import Study from "./Study";
import "./styles.css";

// #/study is the volunteer typing study: no bank account, no passkey. #/demo is the three-bank network demo
// (one-click logins, only when the API runs with DEMO_LOGIN=1). Everything else is the bank.
function Root() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  if (hash.startsWith("#/study")) return <Study />;
  if (hash.startsWith("#/demo")) return <Demo />;
  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
