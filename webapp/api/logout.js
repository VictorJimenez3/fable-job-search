module.exports = (req, res) => {
  res.setHeader("Set-Cookie",
    "jr_s=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0");
  res.writeHead(302, { Location: "/" });
  res.end();
};
