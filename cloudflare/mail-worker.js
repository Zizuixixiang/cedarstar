export default {
  async email(message, env, ctx) {
    const to = String(message.to || "").trim().toLowerCase();
    const target =
      to === "clio@cedarstar.org"
        ? env.CEDARCLIO_INBOX_URL
        : to === "sirius@cedarstar.org"
          ? env.CEDARSTAR_INBOX_URL
          : "";
    if (!target) {
      message.setReject("recipient is not configured");
      return;
    }

    const raw = await new Response(message.raw).arrayBuffer();
    ctx.waitUntil(
      fetch(target, {
        method: "POST",
        headers: {
          "content-type": "message/rfc822",
          "x-mail-secret": env.MAIL_SECRET || "",
        },
        body: raw,
      }).then((resp) => {
        if (!resp.ok) {
          throw new Error(`mail inbox HTTP ${resp.status}`);
        }
      }),
    );
  },
};
