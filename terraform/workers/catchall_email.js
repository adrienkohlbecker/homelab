export default {
  async email(message, env) {
    const local = message.to.split("@")[0].split(/[.+]/)[0].toLowerCase();
    const targets = Object.prototype.hasOwnProperty.call(env.ROUTES, local) ? env.ROUTES[local] : null;
    if (!Array.isArray(targets)) return message.setReject("Address not allowed");
    await Promise.all(targets.map((t) => message.forward(t)));
  },
};
