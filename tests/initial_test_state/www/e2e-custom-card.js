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
window.customCards = window.customCards || [];
window.customCards.push({
  type: "e2e-custom-card",
  name: "E2E Custom Card",
  description: "Test card for ha-mcp's card checks.",
});
