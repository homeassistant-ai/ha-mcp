import fs from 'fs';
import { BMPEncoder } from './bmp.js';

function write(path, buf) {
  fs.writeFileSync(path, buf);
  console.log('Wrote', path, buf.length, 'bytes');
}

// Grayscale gradient
{
  const width = 256;
  const height = 64;
  const data = new Uint8Array(width * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      data[y * width + x] = x & 0xff;
    }
  }
  const enc = new BMPEncoder(width, height, 8);
  const buf = enc.encode(data);
  write('./out_gray.bmp', buf);
}

// Binary checkerboard
{
  const width = 128;
  const height = 128;
  const data = new Uint8Array(width * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      data[y * width + x] = ((x >> 4) + (y >> 4)) % 2 ? 0xFF : 0x00;
    }
  }
  const enc = new BMPEncoder(width, height, 1);
  const buf = enc.encode(data);
  write('./out_binary.bmp', buf);
}

console.log('Done');
