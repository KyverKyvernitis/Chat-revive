import assert from "node:assert/strict";
import test from "node:test";
import { mapWithConcurrency, permissionFromRoles, userCanManageGuild } from "../src/services/discordAuthService.js";

test("considera as permissões do cargo @everyone", () => {
  const permissions = permissionFromRoles([], [
    { id: "guild", name: "@everyone", permissions: "32" },
    { id: "other", name: "Outro", permissions: "8" },
  ], null, "user");
  assert.equal(permissions, 32n);
});

test("limita chamadas concorrentes e preserva a ordem", async () => {
  let active = 0;
  let maximum = 0;
  const result = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    maximum = Math.max(maximum, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    return value * 2;
  });
  assert.deepEqual(result, [2, 4, 6, 8, 10]);
  assert.ok(maximum <= 2);
});

test("autoriza convite pela permissão OAuth mesmo sem o bot no servidor", () => {
  assert.equal(userCanManageGuild([
    { id: "111111111111111", owner: false, permissions: String(1n << 5n) },
  ], "111111111111111"), true);
  assert.equal(userCanManageGuild([
    { id: "222222222222222", owner: false, permissions: "0" },
  ], "222222222222222"), false);
});
