#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Lets encrypt renew started."
br

run "docker pull akohlbecker/letsencrypt"
run "docker run --rm --log-driver syslog --log-opt tag=\"letsencrypt.cron\" --volume /mnt/docker/letsencrypt/etc:/etc/letsencrypt --volume /mnt/docker/letsencrypt/www:/var/www/letsencrypt akohlbecker/letsencrypt letsencrypt renew --agree-tos --non-interactive"

run "systemctl restart nginx"

deadmansnitch "4bf80994fe"
log "Done"
exit 0
