python play_with_glue.py --mode=attack --task_id=IMDB --arch=cnn --embed=w --adv_iter=0 --nbr_2nd=11 --adv_policy=diy --attack_method=pwws --data_split=dev --data_downsample=500 --data_random=True --pred_ensemble=16 --dir_alpha=1.0 --dir_decay=1.0 > 22.log
python play_with_glue.py --mode=attack --task_id=IMDB --arch=cnn --embed=w --adv_iter=0 --nbr_2nd=11 --adv_policy=diy --attack_method=genetic_nolm --data_split=dev --data_downsample=500 --data_random=True --pred_ensemble=16 --dir_alpha=1.0 --dir_decay=1.0 > 33.log
