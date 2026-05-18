export default {
  async email(message, env) {
    const local = message.to.split("@")[0].split(".")[0].toLowerCase();
    const targets = env.ROUTES[local];
    if (!targets) return message.setReject("Address not allowed");
    await Promise.all(targets.map((t) => message.forward(t)));
  },
};
