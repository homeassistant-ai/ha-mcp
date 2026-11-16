// A Z-Wave JS server whose driver talks to zwave-js's mock controller in this
// process, the way zwave-js's own integration tests wire them: no stick, and no
// serial link whose chunking the mock controller would have to survive.
import { ZwavejsServer } from "@zwave-js/server";
import { MockController, MockNode } from "@zwave-js/testing";
import {
  createAndStartDriverWithMockPort,
  createDefaultMockControllerBehaviors,
  createDefaultMockNodeBehaviors,
} from "zwave-js/Testing";

const BINARY_SWITCH = 0x25;
const CONFIGURATION = 0x70;
const VERSION = 0x86;

// One mains-powered switch with two configuration parameters.
const NODES = [
  {
    id: 2,
    capabilities: {
      commandClasses: [
        { ccId: BINARY_SWITCH, isSupported: true, version: 1 },
        {
          ccId: CONFIGURATION,
          isSupported: true,
          version: 4,
          parameters: [
            {
              "#": 1,
              valueSize: 1,
              name: "LED brightness",
              minValue: 0,
              maxValue: 10,
              defaultValue: 3,
            },
            {
              "#": 2,
              valueSize: 2,
              name: "Report interval",
              minValue: 30,
              maxValue: 3600,
              defaultValue: 300,
            },
          ],
        },
        { ccId: VERSION, isSupported: true, version: 2 },
      ],
    },
  },
];

// S2 inclusion needs keys; these protect nothing.
const key = (fill) => Buffer.alloc(16, fill);

const { driver, continueStartup, mockPort, serial } =
  await createAndStartDriverWithMockPort({
    securityKeys: {
      S0_Legacy: key(1),
      S2_Unauthenticated: key(2),
      S2_Authenticated: key(3),
      S2_AccessControl: key(4),
    },
    storage: { cacheDir: "/tmp/zwave-cache" },
    logConfig: { enabled: false },
  });
driver.on("error", (error) => console.error("driver error:", error));

const controller = await MockController.create({
  homeId: 0x7e570001,
  ownNodeId: 1,
  mockPort,
  serial,
});
controller.defineBehavior(...createDefaultMockControllerBehaviors());
for (const options of NODES) {
  const node = await MockNode.create({ ...options, controller });
  controller.addNode(node);
  node.defineBehavior(...createDefaultMockNodeBehaviors());
}

driver.once("driver ready", async () => {
  const server = new ZwavejsServer(driver, {
    port: 3000,
    host: "0.0.0.0",
    enableDNSServiceDiscovery: false,
  });
  await server.start();
  // tests/src/e2e/utilities/zwave_mock.py waits for this line.
  console.log("zwave mock listening");
});
continueStartup();
