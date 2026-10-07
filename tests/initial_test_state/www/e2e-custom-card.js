// A minimal custom card for the card-definition E2E tests (#2632): it
// registers like a HACS card and rejects a config without an entity.
// Module syntax must reach the module evaluator, not classic-script eval.
void import.meta;
export class E2ECustomCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("e2e-custom-card needs an entity");
    this._config = config;
  }

  static getConfigForm() {
    return { schema: [{ name: "entity", required: true, selector: { entity: {} } }] };
  }

  static getConfigElement() {
    return {
      setConfig(config) {
        if (config.disabled !== undefined) {
          throw new Error("At path: disabled -- Expected a value of type `never`");
        }
      },
    };
  }
}
customElements.define("e2e-custom-card", E2ECustomCard);
class E2EBrokenEditorCard extends E2ECustomCard {
  static getConfigElement() { throw new Error("editor unavailable"); }
  static getConfigForm() { return null; }
}
class E2ESlowEditorCard extends HTMLElement {
  setConfig(config) {}
  static getConfigElement() {
    const tick = () => setTimeout(tick, 0);
    tick();
    return Promise.resolve({ setConfig(config) {} });
  }
}
class E2ESlowVerdictCard extends HTMLElement {
  setConfig(config) {}
  static getConfigElement() {
    return { setConfig(config) { while (true) {} } };
  }
}
customElements.define("e2e-broken-editor-card", E2EBrokenEditorCard);
customElements.define("e2e-slow-editor-card", E2ESlowEditorCard);
customElements.define("e2e-slow-verdict-card", E2ESlowVerdictCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "e2e-custom-card",
  name: "E2E Custom Card",
  description: "Test card for ha-mcp's card checks.",
});
