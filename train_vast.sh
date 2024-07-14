# train building
#nohup python train_vast.py -s ../datasets/Mill19/building \
#--exp_name building \
#--manhattan \
#--eval \
#--llffhold 83 \
#--resolution 4 \
#--pos "-62.527942657471 0.000000000000 -15.786898612976" \
#--rot "0.932374119759 0.000000000000 0.361494839191 0.000000000000 1.000000000000 0.000000000000 -0.361494839191 0.000000000000 0.932374119759" \
#--m_region 3 \
#--n_region 3 \
#--iterations 60_000 \
#> log-6.23 2>&1 & echo $! > run.pid


# train rubble cc
#nohup python train_vast.py -s ../datasets/Mill19/rubble \
#--exp_name rubble \
#--manhattan \
#--eval \
#--llffhold 83 \
#--resolution 4 \
#--pos "25.607364654541 0.000000000000 -12.012700080872" \
#--rot "0.923032462597 0.000000000000 0.384722054005 0.000000000000 1.000000000000 0.000000000000 -0.384722054005 0.000000000000 0.923032462597" \
#--m_region 3 \
#--n_region 3 \
#--iterations 60_000 \
#> log-6.25 2>&1 & echo $! > run.pid

# train rubble tj
# nohup python train_vast.py -s ../datasets/Mill19/rubble \
# --exp_name rubble_tj \
# --manhattan \
# --plantform "tj" \
# --eval \
# --llffhold 83 \
# --resolution 4 \
# --pos "0.0 0.0 0.0" \
# --rot "0.0 21 0.0" \
# --m_region 3 \
# --n_region 4 \
# --iterations 60_000 \
# > log-6.26 2>&1 & echo $! > run.pid

# train on your own custom drone footage
python train_vast.py -s ./datasets/yard_new_app/undistorted \
--exp_name yard-final \
--plantform "tj" \
--eval \
--llffhold 10 \
--resolution 2 \
--pos "0 0 0" \
--rot "0 0 0" \
--m_region 1 \
--n_region 1 \
--iterations 60_000 \

# train train
#nohup python train_vast.py -s ../datasets/tandt/train \
#--exp_name train \
#--manhattan \
#--pos "-7.70662571 4.45195873 0.51466437" \
#--rot "0.86119716 0.03436749 0.50710779 0.03316087 0.99414171 -0.1028718 -0.50768368 0.10611482 0.85498364" \
#--m_region 2 \
#--n_region 1 \
#--iterations 30_000 \
#> log-6.23 2>&1 & echo $! > run.pid


#nohup ${command} > log-6.23 2>&1 & echo $! > run.pid
