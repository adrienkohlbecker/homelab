// const fs = require('fs');
// const process = require('process');
const express = require('express');
// const SerialPort = require('serialport');
const MarantzDenonTelnet = require('marantz-denon-telnet');
const req = require('request');

const port = 3000;
// const serialPath = "/dev/ttyACM0";

const marantz = new MarantzDenonTelnet("10.123.30.121");

// try {
//   fs.accessSync(serialPath, fs.constants.R_OK | fs.constants.W_OK);
// } catch (err) {
//   console.error(`No access to ${serialPath}, are you root?`);
//   process.exit(1);
// }

// const serialPort = new SerialPort(serialPath, { baudRate: 115200 })
// serialPort.on('error', function (err) {
//   console.log('Error: ', err.message)
// })

const app = express();
app.use(express.json());

function write(text) {
  serialPort.write(text, function (err) {
    if (err) {
      return console.log('Error on write: ', err.message)
    }
  });
}

// var playingSerial = false;
var playingTelnet = false;

// function setPlayingSerial() {
//   if (!playingSerial) {
//     playingSerial = true;
//     console.log("PLAYING OFFICE");
//     write('$I5\r\n');
//   } else {
//     console.log("debouncing playing");
//   }
// }

// function setStoppedSerial() {
//   if (playingSerial) {
//     playingSerial = false;
//     console.log("STOPPED OFFICE");
//     write('$I1\r\n');
//   } else {
//     console.log("debouncing stopped");
//   }
// }

let wantStopped = false;

function setPlayingTelnet() {
  if (!playingTelnet) {
    if (wantStopped) {
      wantStopped = false
      playingTelnet = true
      console.log("cancelling stop")
    } else {
      playingTelnet = true;
      console.log("PLAYING LIVING ROOM");
      marantz.cmd('PWON', function (error, ret) {
        error ? console.log(error) : marantz.cmd('SICD', function (error, ret) {
          error ? console.log(error) : setTimeout(function () {
            marantz.cmd('MV50', function (error, ret) {
              console.log((error ? error : 'Sent command to turn AVR on.'));
            })
          }, 2000);
        })
      });
    }
  } else {
    console.log("debouncing playing");
  }
}

function setStoppedTelnet() {
  if (playingTelnet) {
    playingTelnet = false;
    wantStopped = true;
    console.log("Stopping in 5 seconds")
    setTimeout(function () {
      if (wantStopped) {
        wantStopped = false
        console.log("STOPPED LIVING ROOM");
        marantz.cmd('PWSTANDBY', function (error, ret) {
          console.log((error ? error : 'Sent command to turn AVR off.'));
        });
      }
    }, 5000);
  } else {
    console.log("debouncing stopped");
  }
}

// Access the parse results as request.body
app.post('/', function (request, _) {
  if (request.body && request.body.type === "transport-state") {
    console.log("Webhook..." + request.body.type + ' ' + (request.body.data.state.playbackState || '') + ' ' + (request.body.data.roomName || ' '));

    zone_uuid = request.body.data.uuid

    req.get("http://localhost:5005/zones", { json: true }, function (err, res, body) {
      if (err) { console.log(err); return }

      body.forEach(zone => {
        if (zone.uuid = zone_uuid) {

          zone.members.forEach(member => {
            if (member.roomName === "Office") {
              // if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
              //   setPlayingSerial();
              // } else {
              //   setStoppedSerial();
              // }
            } else if (member.roomName === "Living Room") {
              console.log('playingTelnet ' + playingTelnet + ' wantStopped ' + wantStopped)
              if (request.body.data.state && request.body.data.state.playbackState && request.body.data.state.playbackState === "PLAYING") {
                setPlayingTelnet();
              } else {
                setStoppedTelnet();
              }
            }
          })
        }
      });
    });
  }
});

app.listen(port, () => console.log(`Example app listening at http://localhost:${port}`))
