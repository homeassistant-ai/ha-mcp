export class CannotOpenPageError extends Error {
  constructor(status, pagePath) {
    super(`Unable to open page: ${pagePath} (${status})`);
    this.status = status;
    this.pagePath = pagePath;
    this.name = "CannotOpenPageError";
  }
}
