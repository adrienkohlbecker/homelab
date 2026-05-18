export default {
  async email(message, env, ctx) {
    if (message.to.startsWith('ak@') || message.to.startsWith('ak.')) {
      await message.forward("adrien.kohlbecker@gmail.com");
    } else if (message.to.startsWith('sp@') || message.to.startsWith('md.')) {
      await message.forward("spouse@example.com");
    } else if (message.to.startsWith('cp@') || message.to.startsWith('am.')) {
      await message.forward("adrien.kohlbecker@gmail.com");
      await message.forward("spouse@example.com");
    } else {
      message.setReject("Address not allowed");  
    }
  }
}